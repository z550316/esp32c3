# ESP32C3 串口监视器

import serial
import time

SERIAL_PORT = 'COM4'
BAUD_RATE = 115200

def main():
    print("=" * 60)
    print("ESP32C3 串口监视器")
    print("=" * 60)
    print("按 Ctrl+C 退出")
    print()
    
    try:
        print(f"连接串口 {SERIAL_PORT}...")
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        print("✓ 已连接")
        print()
        
        while True:
            if ser.in_waiting > 0:
                data = ser.read(ser.in_waiting)
                try:
                    text = data.decode('utf-8', errors='ignore')
                    print(text, end='')
                except:
                    # 显示十六进制
                    hex_str = ' '.join(f'{b:02X}' for b in data)
                    print(f"[{hex_str}]", end='')
            
            time.sleep(0.01)
            
    except KeyboardInterrupt:
        print("\n\n退出监视器")
    except Exception as e:
        print(f"\n错误: {e}")
    finally:
        if 'ser' in locals():
            ser.close()
            print("串口已关闭")

if __name__ == "__main__":
    main()
