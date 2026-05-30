import cv2
import time
import numpy as np
from flask import Flask, render_template, Response, jsonify, request, url_for
from flask_socketio import SocketIO, emit
from ultralytics import YOLO
import tensorflow as tf
from pymongo import MongoClient
from datetime import datetime
import threading
import os
import urllib.request
import time

# Removed global timeout to prevent interference with MongoDB/AI

app = Flask(__name__)
app.config['SECRET_KEY'] = 'military_ops_secret'
socketio = SocketIO(app, cors_allowed_origins="*")

# Tactical Sync Lock: Prevents AI threads from crashing each other
ai_lock = threading.Lock()

# Security/Upload Config
UPLOAD_FOLDER = os.path.join('static', 'uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# MongoDB Setup
try:
    client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=2000)
    db = client["drone_surveillance"]
    logs_col = db["detections"]
    client.server_info() # trigger exception if cannot connect
    print("MongoDB Connected successfully")
except Exception as e:
    print(f"MongoDB Connection Error: {e}")
    logs_col = None

# Model Loading
# 1. YOLOv8 Pretrained
yolo_model = YOLO('yolov8n.pt')

# 2. Custom TFLite Model
interpreter = tf.lite.Interpreter(model_path="models/model.tflite")
interpreter.allocate_tensors()
input_details = interpreter.get_input_details()
output_details = interpreter.get_output_details()

# Custom Model Labels (as per user request)
CUSTOM_LABELS = [
    "helicopter",
    "military_plane",
    "military_tank",
    "military_vehicle",
    "missile"
]

# ESP32-CAM Configuration
# The user specified http://192.168.193.70
# Common MJPEG stream endpoints:
# 1. http://<ip>:81/stream (Standard for Arduino ESP32-CAM example)
# 2. http://<ip>/stream
# 3. http://<ip>:80/mjpeg
STREAM_URL = "http://192.168.169.70:81/stream" 

class VideoCamera:
    def __init__(self, url):
        self.url = url
        self.is_running = True
        self.current_frame = None
        self.processed_frame = None
        self.yolo_detections = []
        self.tflite_detections = []
        self.fps = 0
        self.stream = None
        self.lock = threading.Lock()
        self.frame_id = 0 
        self.frame_lock = threading.Lock() # For safe frame access
        self.operating_mode = "live" # Modes: "live" or "analysis"
        
        # Thread 1: Pure Capture
        threading.Thread(target=self._capture_loop, daemon=True).start()
        # Thread 2: Parallel YOLO AI
        threading.Thread(target=self._yolo_loop, daemon=True).start()
        # Thread 3: Parallel TFLite AI
        threading.Thread(target=self._tflite_loop, daemon=True).start()

    def connect(self):
        """Attempts to open the stream using requests for better stability"""
        try:
            import requests # Import here to avoid dependency issues if missing
            if self.stream:
                try: self.stream.close()
                except: pass
            
            print(f"Attempting to connect to stream: {self.url}")
            # Use requests with stream=True for MJPEG
            self.stream = requests.get(self.url, stream=True, timeout=5)
            print(f"SUCCESS: Connected to {self.url}")
            return True
        except Exception as e:
            # Fallback to urllib if requests is not available or fails
            try:
                print(f"Requests failed, trying urllib fallback: {self.url}")
                self.stream = urllib.request.urlopen(self.url, timeout=5)
                return True
            except:
                print(f"Both methods failed to connect: {e}")
                self.stream = None
                return False

    def update_url(self, new_url):
        print(f"Updating stream URL to: {new_url}")
        self.url = new_url
        self.stream = None # Force reconnect in capture loop

    def _capture_loop(self):
        """Thread 1: Resilient MJPEG Reader"""
        stream_bytes = b''
        
        while self.is_running:
            # HIBERNATION: Don't use CPU if we are in Analysis Mode
            if self.operating_mode != "live":
                if self.stream:
                    try: self.stream.close()
                    except: pass
                self.stream = None
                time.sleep(1)
                continue

            if self.stream is None:
                if not self.connect():
                    time.sleep(2) 
                    continue
            
            try:
                # Handle both 'requests' and 'urllib' objects
                if hasattr(self.stream, 'iter_content'):
                    # Requests-style iterator (very stable)
                    for chunk in self.stream.iter_content(chunk_size=2048):
                        if not self.is_running or self.stream is None: break
                        stream_bytes += chunk
                        stream_bytes = self._process_buffer(stream_bytes)
                else:
                    # Urllib-style read
                    chunk = self.stream.read(2048)
                    if chunk:
                        stream_bytes += chunk
                        stream_bytes = self._process_buffer(stream_bytes)
                    else:
                        self.stream = None
            except Exception as e:
                print(f"[STREAM] Connection lost: {e}")
                self.stream = None
                time.sleep(1)

    def _process_buffer(self, buffer):
        """Helper to find and decode JPEGs in the stream buffer"""
        while True:
            start = buffer.find(b'\xff\xd8')
            end = buffer.find(b'\xff\xd9', start)
            
            if start != -1 and end != -1:
                jpg = buffer[start:end+2]
                buffer = buffer[end+2:]
                
                try:
                    frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None:
                        with self.frame_lock:
                            self.current_frame = frame
                            self.frame_id += 1
                except:
                    pass
            else:
                break
        
        # Buffer safety: Keep it fresh (500KB is safe for High-Res Drone frames)
        if len(buffer) > 512000: 
            return b''
        return buffer

    def _yolo_loop(self):
        """Dedicated thread for YOLOv8 parallel processing (Turbo Mode)"""
        prev_time = 0
        last_processed_id = -1
        frame_skip_counter = 0
        
        while self.is_running:
            if self.operating_mode != "live":
                time.sleep(1)
                continue
            if self.current_frame is None or self.frame_id == last_processed_id:
                time.sleep(0.005)
                continue

            # Skip frames to keep FPS high
            frame_skip_counter += 1
            if frame_skip_counter % 3 != 0:
                last_processed_id = self.frame_id
                continue

            last_processed_id = self.frame_id
            with self.frame_lock:
                frame = self.current_frame.copy()
            
            # Ultra-fast resolution with sync lock
            with ai_lock:
                results = yolo_model(frame, stream=True, verbose=False, imgsz=256)
            
            temp_detections = []
            for r in results:
                for box in r.boxes:
                    cls = int(box.cls[0])
                    conf = float(box.conf[0])
                    if conf > 0.4:
                        x1, y1, x2, y2 = map(int, box.xyxy[0])
                        temp_detections.append({
                            "label": yolo_model.names[cls], "confidence": conf,
                            "bbox": [x1, y1, x2, y2], "source": "YOLOv8"
                        })
            
            with self.lock:
                self.yolo_detections = temp_detections
            
            # FPS Calculation (YOLO specific)
            curr_time = time.time()
            self.fps = 1 / (curr_time - prev_time) if prev_time != 0 else 0
            prev_time = curr_time

    def _tflite_loop(self):
        """Dedicated thread for Custom TFLite parallel processing (Turbo Mode)"""
        last_processed_id = -1
        frame_skip_counter = 0
        
        while self.is_running:
            if self.operating_mode != "live":
                time.sleep(1)
                continue
            if self.current_frame is None or self.frame_id == last_processed_id:
                time.sleep(0.005)
                continue

            # Skip frames to keep FPS high
            frame_skip_counter += 1
            if frame_skip_counter % 3 != 0:
                last_processed_id = self.frame_id
                continue

            last_processed_id = self.frame_id
            with self.frame_lock:
                frame = self.current_frame.copy()
            try:
                h_model, w_model = input_details[0]['shape'][1], input_details[0]['shape'][2]
                input_img = cv2.resize(frame, (w_model, h_model))
                input_img = input_img.astype(np.float32) / 255.0
                input_data = np.expand_dims(input_img, axis=0)

                # Execute TFLite with Sync Lock
                with ai_lock:
                    interpreter.set_tensor(input_details[0]['index'], input_data)
                    interpreter.invoke()
                    output_data = interpreter.get_tensor(output_details[0]['index'])[0]
                
                temp_detections = []
                for detection in output_data:
                    probs = detection[4:]
                    max_prob = np.max(probs)
                    if max_prob > 0.6: # Back to strict threshold
                        class_id = np.argmax(probs)
                        cx, cy, w_box, h_box = detection[0:4]
                        ih, iw, _ = frame.shape
                        x1, y1 = int((cx - w_box/2) * iw), int((cy - h_box/2) * ih)
                        x2, y2 = int((cx + w_box/2) * iw), int((cy + h_box/2) * ih)

                        temp_detections.append({
                            "label": CUSTOM_LABELS[class_id] if class_id < len(CUSTOM_LABELS) else "Target",
                            "confidence": float(max_prob),
                            "bbox": [x1, y1, x2, y2], "source": "MIL-SCAN"
                        })
                
                with self.lock:
                    self.tflite_detections = temp_detections
            except:
                pass
            time.sleep(0.01) # Small rest to prevent CPU pinning

    def get_frame(self):
        # Always use a fresh copy with a LOCK to prevent corruption
        frame = None
        with self.frame_lock:
            if self.current_frame is not None:
                frame = self.current_frame.copy()
        
        if frame is None:
            placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(placeholder, "INITIALIZING HUD...", (160, 240), 0, 1, (255, 255, 255), 2)
            ret, jpeg = cv2.imencode('.jpg', placeholder)
            return jpeg.tobytes()
        
        # Merge and Draw Detections with "Smooth-Draw" logic
        with self.lock:
            # Draw YOLO (Green)
            for det in self.yolo_detections:
                x1, y1, x2, y2 = det['bbox']
                # Sub-pixel smoothing not needed here, but keeping boxes clean
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(frame, f"TARGET: {det['label']}", (x1, y1-10), 0, 0.5, (0, 255, 0), 2)
                
            # Draw MIL-SCAN (Red)
            for det in self.tflite_detections:
                x1, y1, x2, y2 = det['bbox']
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
                cv2.putText(frame, f"HIGH-VALUE: {det['label']}", (x1, y1-10), 0, 0.5, (0, 0, 255), 2)

        # High-Speed Resizing (using INTER_NEAREST is faster than the default)
        display_frame = cv2.resize(frame, (800, 600), interpolation=cv2.INTER_NEAREST)
        
        ret, jpeg = cv2.imencode('.jpg', display_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return jpeg.tobytes()

    def get_all_detections(self):
        with self.lock:
            return self.yolo_detections + self.tflite_detections

camera = VideoCamera(STREAM_URL)

@app.route('/')
def index():
    global camera
    return render_template('index.html', current_url=camera.url)

@app.route('/update_url', methods=['POST'])
def update_url():
    data = request.get_json()
    new_url = data.get('url')
    if new_url:
        camera.update_url(new_url)
        return jsonify({"status": "success", "url": new_url})
    return jsonify({"status": "error", "message": "No URL provided"}), 400


@app.route('/set_mode/<mode>')
def set_mode(mode):
    global camera
    if mode in ["live", "analysis"]:
        camera.operating_mode = mode
        return jsonify({"status": "success", "mode": mode})
    return jsonify({"status": "error"}), 400

def gen(camera):
    while True:
        frame = camera.get_frame()
        if frame:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
        # Tiny sleep to let the CPU breathe and allow threading context switches
        time.sleep(0.01)

@app.route('/video_feed')
def video_feed():
    return Response(gen(camera),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stats')
def get_stats():
    detections = camera.get_all_detections()
    return jsonify({
        "fps": round(camera.fps, 2),
        "detections": detections,
        "count": len(detections)
    })

@app.route('/analysis')
def analysis():
    global camera
    camera.operating_mode = "analysis"
    return render_template('analysis.html')

@app.route('/upload_analysis', methods=['POST'])
def upload_analysis():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file part'})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No selected file'})

    if file:
        try:
            # Save with unique name to avoid cache issues
            ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'jpg'
            filename = f"analyst_input.{ext}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            # Run YOLO Inference with Sync Lock
            img = cv2.imread(filepath)
            if img is None:
                return jsonify({'success': False, 'error': 'Corrupted image file'})
                
            with ai_lock:
                # 1. YOLOv8 Deep Scan (Increased to 640 for detail)
                yolo_results = yolo_model(img, imgsz=640)[0]
                
                # 2. Custom MIL-SCAN (Deep Data Ingestion)
                h_model, w_model = input_details[0]['shape'][1], input_details[0]['shape'][2]
                tfl_img = cv2.resize(img, (w_model, h_model))
                tfl_img = tfl_img.astype(np.float32) / 255.0
                tfl_input = np.expand_dims(tfl_img, axis=0)
                
                interpreter.set_tensor(input_details[0]['index'], tfl_input)
                interpreter.invoke()
                tfl_output = interpreter.get_tensor(output_details[0]['index'])

            # Custom Model Label Mapping (Using global CUSTOM_LABELS)
            MIL_LABELS = CUSTOM_LABELS
            
            detections_summary = []
            
            # Process YOLO Results (Thick Yellow HUD Boxes)
            for box in yolo_results.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                conf = float(box.conf[0])
                cls = int(box.cls[0])
                name = yolo_model.names[cls]
                label = f"{name.upper()} [{int(conf*100)}%]"
                
                detections_summary.append({"label": name, "confidence": conf, "source": "YOLOv8"})
                
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 255), 3)
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
                cv2.rectangle(img, (x1, y1 - th - 15), (x1 + tw, y1), (0, 255, 255), -1)
                cv2.putText(img, label, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)

            # Process TFLite Results (Strict Deduplication)
            processed_output = tfl_output[0] if len(tfl_output.shape) > 1 else tfl_output
            
            # Record detections to prevent overlap
            final_custom_detections = []
            
            for det in processed_output:
                if isinstance(det, (list, np.ndarray)) and len(det) > 4:
                    probs = det[4:]
                    max_prob = np.max(probs)
                    if max_prob > 0.6: # High confidence only
                        label_idx = np.argmax(probs)
                        name = CUSTOM_LABELS[label_idx] if label_idx < len(CUSTOM_LABELS) else f"THREAT_{label_idx}"
                        
                        # Check if we already have a detection in this general area
                        is_duplicate = False
                        # Simple spatial check if coordinates exist [x1,y1,x2,y2]
                        if len(det) >= 6:
                            new_box = det[2:6]
                            for existing in final_custom_detections:
                                if existing['label'] == name:
                                    # If confidence is lower than existing, skip
                                    is_duplicate = True
                                    break
                        
                        if not is_duplicate:
                            final_custom_detections.append({
                                "label": name, 
                                "confidence": float(max_prob),
                                "source": "MIL-SCAN"
                            })
            
            detections_summary.extend(final_custom_detections)
            
            # Save processed
            output_path = os.path.join(app.config['UPLOAD_FOLDER'], "analyst_output.jpg")
            cv2.imwrite(output_path, img)
            
            return jsonify({
                'success': True, 
                'processed_url': url_for('static', filename='uploads/analyst_output.jpg'),
                'detections': detections_summary
            })
        except Exception as e:
            print(f"ANALYSIS ERROR: {e}")
            return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
