#in same directory as templates, create a file name combine1.py:
from flask import Flask, render_template, request, redirect, jsonify
import json
import os
import RPi.GPIO as GPIO
from time import sleep, time
import threading
import Adafruit_DHT
import spidev
import I2C_LCD_driver
import requests
from datetime import datetime, timedelta

app = Flask(__name__)


GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)


GPIO.setup(4, GPIO.IN)    # Moisture sensor 
GPIO.setup(26, GPIO.OUT)  # Servo 
servo_pwm = GPIO.PWM(26, 50)
servo_pwm.start(3)        # Initial position

# DHT
DHT_SENSOR = Adafruit_DHT.DHT11
DHT_PIN = 21

# LDR Sensor
spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 1350000
GPIO.setup(24, GPIO.OUT)  # LDR LED control


lcd = I2C_LCD_driver.lcd()

# Water Level Sensor
GPIO.setup(25, GPIO.OUT)  
GPIO.setup(27, GPIO.IN)   


SYSTEM_STATE_FILE = "system_state.json"
DEFAULT_STATE = {"system": True, "temperature_humidity": True, "ldr": True}
THINGSPEAK_API_KEY = "ATNCBN0ZUFSYGREX" # Write api key
TELEGRAM_TOKEN = "7094057858:AAGU0CMWAcTnuMBJoUmBlg8HxUc8c1Mx3jw"
CHAT_ID = "-1002405515611"


sdelay = 4  # Servo delay
last_temp_alert_time = None
last_humidity_alert_time = None
last_thingspeak_upload_time = None


def read_system_state():
    if not os.path.exists(SYSTEM_STATE_FILE):
        with open(SYSTEM_STATE_FILE, "w") as f:
            json.dump(DEFAULT_STATE, f)
    with open(SYSTEM_STATE_FILE, "r") as f:
        return json.load(f)

def write_system_state(state):
    with open(SYSTEM_STATE_FILE, "w") as f:
        json.dump(state, f)

def readadc(adcnum):
    r = spi.xfer2([1, (8 + adcnum) << 4, 0])
    return ((r[1] & 3) << 8) + r[2]

def distance():
    GPIO.output(25, True)
    sleep(0.00001)
    GPIO.output(25, False)
    
    start_time = time()
    stop_time = time()
    
    while GPIO.input(27) == 0:
        start_time = time()
        
    while GPIO.input(27) == 1:
        stop_time = time()
        
    return (stop_time - start_time) * 34300 / 2

def can_send_alert(last_alert_time):
    if last_alert_time is None:
        return True
    return datetime.now() - last_alert_time > timedelta(hours=24)

def upload_to_thingspeak(temp, humi):
    global last_thingspeak_upload_time
    if last_thingspeak_upload_time is None or (datetime.now() - last_thingspeak_upload_time).seconds >= 15:
        url = f"https://api.thingspeak.com/update?api_key={THINGSPEAK_API_KEY}&field1={temp}&field2={humi}"
        requests.get(url)
        last_thingspeak_upload_time = datetime.now()

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={CHAT_ID}&text={message}"
    requests.get(url)

def get_last_refill_timestamp():
    try:
        url = f"https://api.thingspeak.com/channels/2746200/feeds.json?results=10&api_key=IJ7JE71BJ5DVEMG7"
        response = requests.get(url)
        if response.status_code == 200:
            records = json.loads(response.text)["feeds"]
            for record in reversed(records):
                if record.get("field3") == "1":
                    return record["created_at"].replace("T", " ").split("+")[0]
        return "Unknown"
    except Exception as e:
        print(f"Error getting refill timestamp: {e}")
        return "Unknown"

def get_days_since_refill():
    last_refill = get_last_refill_timestamp().split(" ")[0]
    if last_refill == "Unknown":
        return "N/A"
    try:
        refill_date = datetime.strptime(last_refill, "%Y-%m-%d").date()
        return (datetime.now().date() - refill_date).days
    except:
        return "N/A"

# -----------------------------background threads--------------------------------------------------
def sensor_loop():
    global last_temp_alert_time, last_humidity_alert_time
    while True:
        state = read_system_state()
        if not state["system"]:
            lcd.lcd_clear()
            lcd.lcd_display_string("System OFF", 1)
            lcd.lcd_display_string("Enable in Web UI", 2)
            sleep(2)
            continue

        
        temp, humi = None, None
        if state["temperature_humidity"]:
            humi, temp = Adafruit_DHT.read(DHT_SENSOR, DHT_PIN)
            if humi is not None and temp is not None:
                upload_to_thingspeak(temp, humi)
                
                if (temp < 18 or temp > 28) and can_send_alert(last_temp_alert_time):
                    send_telegram_alert(f"Temperature alert: {temp}°C")
                    last_temp_alert_time = datetime.now()
                
                if humi > 80 and can_send_alert(last_humidity_alert_time):
                    send_telegram_alert(f"Humidity alert: {humi}%")
                    last_humidity_alert_time = datetime.now()

        ldr_value = None
        if state["ldr"]:
            ldr_value = readadc(0)
            GPIO.output(24, ldr_value < 500)


        lcd.lcd_clear()
        if temp is not None and humi is not None:
            lcd.lcd_display_string(f"T:{temp:.1f}C H:{humi:.1f}%", 1)
        else:
            lcd.lcd_display_string("T:ERR H:ERR", 1)
            
        if state["ldr"]:
            lcd.lcd_display_string(f"LDR:{ldr_value}", 2)
        else:
            lcd.lcd_display_string("LDR:OFF", 2)

        sleep(2)

def moisture_detection():
    global sdelay
    while True:
        if GPIO.input(4):
            
            servo_pwm.ChangeDutyCycle(8)
            sleep(2)
            servo_pwm.ChangeDutyCycle(0)
            sleep(sdelay-2)
            
            servo_pwm.ChangeDutyCycle(3)
            sleep(2)
            servo_pwm.ChangeDutyCycle(0)
        else:
            sleep(0.5)

def tank_monitor():
    while True:
        level = distance()
        if level > 50:  
            if get_last_refill_timestamp().split(" ")[0] != datetime.now().strftime("%Y-%m-%d"):
                send_telegram_alert("Water tank needs refill!")
            requests.get(f"https://api.thingspeak.com/update?api_key={THINGSPEAK_API_KEY}&field3=1")
        else:
            requests.get(f"https://api.thingspeak.com/update?api_key={THINGSPEAK_API_KEY}&field3=0")
        sleep(60)  


@app.route("/")
def home():
    return render_template("combine1.html",
                         state=read_system_state(),
                         sdelay=sdelay,
                         days=get_days_since_refill(),
                         last_refill=get_last_refill_timestamp())

@app.route("/toggle", methods=["POST"])
def toggle():
    toggle_type = request.form.get("toggle")
    state = read_system_state()
    
    if toggle_type in state:
        state[toggle_type] = not state[toggle_type]
        write_system_state(state)
        print(f"Toggled {toggle_type} to {state[toggle_type]}")
    
    return redirect("/")

@app.route("/set_delay", methods=["POST"])
def set_delay():
    global sdelay
    try:
        new_delay = int(request.form["sdelay"])
        if 4 <= new_delay <= 20:
            sdelay = new_delay
            print(f"Servo delay set to {sdelay}s")
        else:
            print("Invalid delay value")
    except ValueError:
        print("Invalid delay input")
    return redirect("/")

@app.route("/update")
def status_update():
    return jsonify({
        "days_since_refill": get_days_since_refill(),
        "last_refill_timestamp": get_last_refill_timestamp(),
        "tank_level": distance()
    })


if __name__ == "__main__":

    threading.Thread(target=sensor_loop, daemon=True).start()
    threading.Thread(target=moisture_detection, daemon=True).start()
    threading.Thread(target=tank_monitor, daemon=True).start()
    
    # Start Flask app
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)