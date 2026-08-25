# RTO/RPO Evidence — Day 23 Region Failover

Sinh viên: Nguyễn Anh Đức — MSSV 2A202601930 — K4
Drill chạy ngày 25/08/2026, chế độ bare mode với `chaos/kill_region.py --mode netblock --mock`.

Mọi con số trong tài liệu này lấy từ log của chính lần chạy này. Không có số nào
được ước lượng hay chép từ ví dụ minh hoạ trong GUIDE.

## 1. Hai lần drill

| Drill | Có DR chưa | Log loadgen | Request fail | Verdict |
|---|---|---|---|---|
| Drill 1 (baseline) | Chưa | `reports/drill-1-nodr.jsonl` | 15 trên 32 | NO_RECOVERY |
| Drill 2 (sau containment) | Rồi | `reports/drill-2-withdr.jsonl` | 14 trên 158 | PASS |

Drill 1 không bao giờ tự phục hồi: sau khi Region A bị SIGSTOP, không có thành phần
nào phát hiện và không có thành phần nào chuyển traffic. Hệ thống chỉ sống lại khi
người vận hành gõ tay lệnh restore. Đó là ý nghĩa thực tế của RTO bằng vô hạn.

## 2. Timeline drill 2 (mốc 0 là lúc Region A chết)

Mốc 0: `chaos/chaos-events.jsonl:5` — kill region-a, mode netblock, lúc 13:39:21 UTC.

| Sự kiện | Kể từ mốc 0 | Evidence |
|---|---:|---|
| User nhận lỗi đầu tiên (ReadTimeout 2018.7ms) | 0.0s | `reports/drill-2-withdr.jsonl:25` |
| Health checker flag region-a UNHEALTHY | 14.9s | `reports/health-events.jsonl:2` |
| Snapshot restore hoàn tất trên Region B | 17.3s | `reports/failover-events.jsonl:2` |
| Pool Region B chuyển warm sang full | 17.3s | `reports/failover-events.jsonl:3` |
| Region B trả /readyz 200 (hết warm-up) | 23.4s | `reports/failover-events.jsonl:4` |
| DNS cutover, edge/active_region ghi "b" | 23.4s | `reports/failover-events.jsonl:5` |
| Request thành công đầu tiên, phục vụ bởi Region B | 28.2s | `reports/drill-2-withdr.jsonl:39` |

RTO đo được: 28.2s. Mục tiêu: 300s. Kết quả PASS, còn dư hơn 90 phần trăm ngân sách.

## 3. RTO gồm những gì

Bốn thành phần dưới đây cộng lại bằng đúng RTO đo được.

| Thành phần | Thời lượng | Vì sao có nó | Evidence |
|---|---:|---|---|
| Detection floor của health check | 14.9s | interval 5.0s nhân threshold 3 bằng 15.0s. Không thể phát hiện nhanh hơn con số này dù failover có nhanh cỡ nào | `reports/health-events.jsonl:2` |
| Restore snapshot vào Region B | 2.4s | Copy vectors.sqlite và model.bin từ state/_replica, cộng thời gian runbook xác nhận và thông báo incident | `reports/failover-events.jsonl:2` |
| GPU pool warm-up | 6.1s | Ghi full vào pool_state không làm region sẵn sàng ngay. serving/app.py chỉ trả /readyz 200 sau khi hết WARMUP_SECONDS | `reports/failover-events.jsonl:4` |
| DNS TTL cache ở edge | 4.8s | edge/proxy.py cache kết quả resolve trong EDGE_TTL_SECONDS bằng 5.0s, nên request đầu sau cutover vẫn còn đi về Region A đã chết | `reports/drill-2-withdr.jsonl:39` |

Tổng: 14.9 cộng 2.4 cộng 6.1 cộng 4.8 bằng 28.2s.

Thành phần lớn nhất là detection floor, chiếm hơn một nửa RTO. Đây là chi phí bắt buộc
của việc chống flapping, không phải overhead có thể bỏ đi.

## 4. RPO

| Chỉ số | Giá trị | Evidence |
|---|---:|---|
| RPO tính bằng thời gian | 4.0s | `reports/failover-events.jsonl:2` |
| Số document mất vĩnh viễn | 2 | `reports/failover-events.jsonl:2` |
| Chu kỳ replication | 30s | `reports/replication.jsonl:2` |
| Embedding model version của bản restore | embed-model=vi-e5-base@v3 | `reports/failover-events.jsonl:2` |

RPO ở đây không phải tuổi của snapshot mà là lượng dữ liệu Region A có nhưng bản
restore không có. Cách tính nằm trong hàm rpo của `state/snapshot.py`, đối chiếu
MAX(ingested_at) của hai database rồi đếm số document vượt quá mốc đó.

Con số 4.0s nhỏ hơn nhiều so với chu kỳ replication 30s vì outage rơi vào thời điểm
gần cuối chu kỳ: snapshot cuối cùng chạy lúc 13:39:34, restore diễn ra lúc 13:39:39.
Nếu outage rơi ngay sau một chu kỳ, RPO sẽ tiến gần 30s và số document mất sẽ tăng
tương ứng theo tốc độ ingest 0.5 doc mỗi giây. RPO vì thế dao động theo thời điểm sự
cố, còn RTO thì ổn định hơn nhiều.

## 5. Cấu hình health check

| Tham số | Giá trị |
|---|---:|
| interval | 5.0s |
| threshold | 3 lần fail liên tiếp |
| detection floor | 15.0s |
| timeout mỗi lần probe | 2.0s |
| endpoint | /readyz trên cả hai region |

Chọn /readyz chứ không phải /healthz vì Region B trong lab này có process sống hoàn
toàn bình thường nhưng vector DB rỗng và không có model weights. Nếu giám sát bằng
/healthz thì hệ thống sẽ tưởng Region B sẵn sàng phục vụ và cutover vào một region
không trả lời được câu hỏi nào.

## 6. Kiểm chứng

Lệnh tái lập toàn bộ con số trong tài liệu này:

    python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300

Công cụ đọc bốn nguồn log độc lập và tự tính lại: `tools/measure_rto.py`.
Kết quả trả về valid true, warnings rỗng, rto_verdict PASS.