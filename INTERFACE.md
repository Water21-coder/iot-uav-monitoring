# Đặc tả giao tiếp dữ liệu (Interface Specification)

Tài liệu này quy định định dạng dữ liệu giữa phần firmware (UAV, trạm mặt đất, trạm ground truth) và phần server (MQTT broker, database, web dashboard). Firmware gửi đúng định dạng này thì server nhận và xử lý được ngay.

## 1. Kiến trúc

```
UAV: LoRa32 #1 + 2 BME280 + Pixhawk
        |  LoRa (chuỗi CSV 13 trường)
        v
Trạm mặt đất: LoRa32 #2  -- thêm RSSI, SNR --> WiFi --> MQTT broker (laptop)
Trạm ground truth: ESP32 + BME280 -------------> WiFi --> MQTT broker (laptop)
```

## 2. Kết nối MQTT

| Mục | Giá trị |
|---|---|
| Broker | Mosquitto chạy trên laptop |
| Host | IP của laptop trong mạng WiFi (hiện tại `192.168.1.39`, sẽ đổi khi dùng router dã ngoại, kiểm tra bằng `ipconfig`) |
| Port | `1883` |
| Đăng nhập | Không cần (giai đoạn test) |
| QoS | `0` |
| Client ID | Mỗi thiết bị một tên riêng: `ground-station`, `ground-truth` |

Nên đặt Host thành một biến ở đầu code để dễ sửa khi đổi mạng.

## 3. Gói tin từ UAV (gửi qua LoRa)

Chuỗi CSV, **13 trường**, cách nhau bằng dấu phẩy, **không có khoảng trắng**, **đúng thứ tự**:

```
seq,gps_time,t1,h1,p1,t2,h2,p2,lat,lon,alt,battery,mode
```

| # | Trường | Ý nghĩa | Đơn vị | Kiểu | Ví dụ |
|---|---|---|---|---|---|
| 1 | seq | Số thứ tự gói, bắt đầu từ 1 mỗi lần bật máy, tăng 1 mỗi lần gửi | - | số nguyên | 125 |
| 2 | gps_time | Thời điểm gửi, lấy từ GPS của Pixhawk, dạng Unix timestamp | giây | số nguyên | 1731234567 |
| 3 | t1 | Nhiệt độ BME280 **có shield** | °C | 2 số lẻ | 31.25 |
| 4 | h1 | Độ ẩm BME280 có shield | % | 2 số lẻ | 68.50 |
| 5 | p1 | Áp suất BME280 có shield | hPa | 2 số lẻ | 1008.30 |
| 6 | t2 | Nhiệt độ BME280 **không shield** | °C | 2 số lẻ | 31.80 |
| 7 | h2 | Độ ẩm BME280 không shield | % | 2 số lẻ | 67.90 |
| 8 | p2 | Áp suất BME280 không shield | hPa | 2 số lẻ | 1008.10 |
| 9 | lat | Vĩ độ GPS | độ | 6 số lẻ | 10.987600 |
| 10 | lon | Kinh độ GPS | độ | 6 số lẻ | 106.654300 |
| 11 | alt | Độ cao so với điểm cất cánh | m | 1 số lẻ | 25.4 |
| 12 | battery | Điện áp pin | V | 2 số lẻ | 11.80 |
| 13 | mode | Chế độ lấy mẫu: `F` = Fixed, `A` = Adaptive | - | 1 ký tự | F |

Ví dụ một gói hoàn chỉnh (khoảng 90 byte):

```
125,1731234567,31.25,68.50,1008.30,31.80,67.90,1008.10,10.987600,106.654300,25.4,11.80,F
```

**Quy tắc quan trọng:**
- `seq` phải tăng liên tục, **không bỏ số**. Server dựa vào số bị thiếu để tính Packet Loss và PDR.
- Khi đổi giữa chế độ F và A, nên khởi động lại để `seq` bắt đầu từ 1. Server tự tách thành chuyến bay mới.
- Nếu GPS chưa fix, vẫn gửi gói nhưng ghi `lat`, `lon` là `0`.
- Nếu lấy được thời gian có mili giây thì báo lại, server sẽ cập nhật để tính Latency chính xác hơn.

## 4. Trạm mặt đất (LoRa32 #2)

Nhiệm vụ:
1. Nhận chuỗi CSV từ LoRa.
2. **Giữ nguyên** 13 trường, nối thêm 2 trường ở cuối: `rssi` và `snr` đo được khi nhận gói đó.
3. Publish ngay lên MQTT.

| Mục | Giá trị |
|---|---|
| Topic | `uav/01/sensor` |
| Payload | Chuỗi CSV 15 trường, dạng text thuần, **không dùng JSON** |

```
seq,gps_time,t1,h1,p1,t2,h2,p2,lat,lon,alt,battery,mode,rssi,snr
```

Ví dụ:

```
125,1731234567,31.25,68.50,1008.30,31.80,67.90,1008.10,10.987600,106.654300,25.4,11.80,F,-90.2,7.5
```

| Trường thêm | Ý nghĩa | Đơn vị | Ví dụ |
|---|---|---|---|
| rssi | Cường độ tín hiệu khi nhận gói | dBm | -90.2 |
| snr | Tỉ số tín hiệu trên nhiễu khi nhận gói | dB | 7.5 |

Không cần thêm thời điểm nhận, server tự ghi.

## 5. Trạm ground truth (ESP32 + BME280)

| Mục | Giá trị |
|---|---|
| Topic | `ground/ref` |
| Chu kỳ gửi | 2 giây |
| Thời gian | Đồng bộ qua NTP khi kết nối WiFi |

Chuỗi CSV **5 trường**:

```
seq,timestamp,temp,hum,pres
```

| # | Trường | Ý nghĩa | Đơn vị | Ví dụ |
|---|---|---|---|---|
| 1 | seq | Số thứ tự gói | - | 57 |
| 2 | timestamp | Thời điểm đo, Unix timestamp từ NTP | giây | 1731234567 |
| 3 | temp | Nhiệt độ | °C | 31.05 |
| 4 | hum | Độ ẩm | % | 68.12 |
| 5 | pres | Áp suất | hPa | 1010.02 |

Ví dụ:

```
57,1731234567,31.05,68.12,1010.02
```

**Lưu ý khi đặt trạm:**
- Đặt trong bóng râm, thoáng gió, cách xa khu vực cánh quạt UAV khi cất cánh.
- **Bật trước** khi UAV cất cánh và **tắt sau** khi UAV hạ cánh.

## 6. Lưu ý cho thí nghiệm RQ1 (shield và downwash)

Trạm ground truth đo ở mặt đất. Khi UAV bay cao, nhiệt độ và độ ẩm thật ở trên cao đã khác dưới đất, nên sai số đo được sẽ lẫn cả chênh lệch theo độ cao, không chỉ do cảm biến hay downwash.

Đề xuất: khi làm thí nghiệm so sánh shield, cho UAV **hover thấp (2 đến 5 m) gần trạm ground truth**, hoặc đặt trạm ground truth trên cột cao gần độ cao bay.

## 7. Cách tự kiểm tra trước khi ghép hệ thống

1. Cài **MQTT Explorer** (https://mqtt-explorer.com), kết nối tới IP laptop, port 1883.
2. Cho thiết bị gửi, xem topic `uav/01/sensor` hoặc `ground/ref` có hiện chuỗi đúng định dạng không.
3. Nếu sai định dạng, cửa sổ `subscriber.py` trên laptop sẽ báo `Skip bad packet` kèm lý do (ví dụ sai số trường).

## 8. Cần nhóm firmware xác nhận

- [ ] Thứ tự 13 trường của UAV như mục 3
- [ ] Quy tắc chế độ Adaptive: điều kiện nào thì gửi (ngưỡng thay đổi cảm biến, ngưỡng RSSI)
- [ ] Tần số LoRa32 đang dùng (433 hay 868/915 MHz)
- [ ] Chu kỳ gửi ở chế độ Fixed (hiện server đang test với 2 giây)
- [ ] Pixhawk xuất thêm được dữ liệu telemetry nào (tốc độ, tư thế bay, chế độ bay)
