# Runbook: Region chính down

Đối tượng sử dụng: kỹ sư trực on-call, không cần là người viết code này.
Điều kiện kích hoạt: cảnh báo region chính UNHEALTHY từ `dr/health_checker.py`.
Thời gian dự kiến hoàn tất: dưới 60 giây.

Trước khi bắt đầu, mở terminal tại thư mục gốc của repo và chạy:

    source .venv/bin/activate

## Bước 1: Xác nhận outage là thật

Chủ sở hữu: kỹ sư on-call.

    python3 chaos/kill_region.py status

Dấu hiệu hoàn tất: region chính trả alive false hoặc ready false, đồng thời region
phụ trả alive true. Kiểm tra chéo bằng log health checker:

    tail -5 reports/health-events.jsonl

Phải thấy dòng có to UNHEALTHY cho region chính. Một lần probe fail không phải outage.
Nếu chỉ có một lần fail rồi tự khỏi, dừng lại và theo dõi thêm.

Nếu cả hai region cùng down, dừng quy trình này ngay. Failover không giải quyết được
sự cố toàn hệ thống, hãy chuyển sang quy trình sự cố hạ tầng.

## Bước 2: Thông báo incident và bắt đầu tính giờ

Chủ sở hữu: kỹ sư on-call.

Đăng vào kênh incident, ghi rõ thời điểm outage lấy từ log chứ không phải thời điểm
bạn nhận ra:

    tail -1 chaos/chaos-events.jsonl

Nội dung thông báo cần có: region nào down, thời điểm outage theo log, số request
đang fail, và tên người đang xử lý.

Dấu hiệu hoàn tất: thông báo đã đăng, có người xác nhận đã đọc.

## Bước 3: Kiểm tra đã có snapshot chưa

Chủ sở hữu: kỹ sư on-call.

    python3 state/snapshot.py lag --backend fs

Dấu hiệu hoàn tất: lệnh trả về rpo_seconds là một con số, không phải null.

Nếu lệnh báo không tìm thấy MANIFEST.json, nghĩa là chưa từng có snapshot nào. Failover
sẽ thất bại ở bước restore. Dừng lại, leo thang cho trưởng nhóm Data Platform.

## Bước 4: Chạy failover

Chủ sở hữu: kỹ sư on-call, cần trưởng nhóm SRE chấp thuận nếu ngoài giờ hành chính.

    python3 dr/runbook.py --primary a --target b --backend fs

Lệnh sẽ hỏi xác nhận y/N trước khi chuyển. Đây là chủ ý, không phải thiếu sót: failover
tự động hoàn toàn không có circuit breaker dễ gây flapping hai chiều giữa hai region.

Gõ y rồi Enter để tiếp tục.

Trong CI hoặc drill có chấm điểm, thêm cờ --auto để bỏ qua bước hỏi.

Dấu hiệu hoàn tất: lệnh in ra JSON có ok true và cutover_ok true. Quá trình mất khoảng
25 giây, phần lớn là chờ region phụ warm-up.

Nếu lệnh dừng ở 4_wait_ready với ok false, region phụ không đạt trạng thái sẵn sàng
trong thời gian chờ. Quy trình đã tự động không cutover, đó là hành vi đúng. Chuyển
sang phần Rollback bên dưới.

## Bước 5: Xác minh dữ liệu ở region phụ

Chủ sở hữu: kỹ sư on-call.

    curl -s localhost:8002/v1/state; echo

Dấu hiệu hoàn tất: count lớn hơn 0, weights true, pool_state full.

Ghi lại rpo_seconds và docs_lost từ output bước 4. Hai con số này phải báo cho chủ sở
hữu dữ liệu vì chúng là lượng dữ liệu mất vĩnh viễn, không khôi phục được.

## Bước 6: Kiểm tra golden signals

Chủ sở hữu: kỹ sư on-call.

    for i in $(seq 1 10); do curl -s localhost:8080/v1/infer | head -c 80; echo; done

Dấu hiệu hoàn tất: ít nhất 9 trên 10 request trả về region b, không có error trong
response body. Vài request đầu có thể còn timeout do DNS cache, đó là bình thường và
sẽ hết sau khoảng 5 giây.

Nếu sau 30 giây vẫn còn lỗi liên tục, chuyển sang phần Rollback.

## Bước 7: Đóng incident và đo RTO

Chủ sở hữu: kỹ sư on-call.

    python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300

Dấu hiệu hoàn tất: lệnh trả về valid true và rto_verdict PASS.

Đăng con số RTO và RPO vào kênh incident. Tạo ticket postmortem trong vòng 24 giờ.
Không đóng incident khi chưa có con số đo được từ log.

## Rollback về region chính

Điều kiện kích hoạt rollback:

Một trong các trường hợp sau xảy ra thì phải rollback:
- Bước 4 dừng ở 4_wait_ready, region phụ không bao giờ sẵn sàng
- Sau bước 6, tỷ lệ lỗi vẫn trên 50 phần trăm sau 30 giây
- Region phụ phục vụ được nhưng dữ liệu thiếu nghiêm trọng, docs_lost vượt ngưỡng
  chấp nhận được của nghiệp vụ

Thẩm quyền quyết định rollback: trưởng nhóm SRE trực. Kỹ sư on-call không tự quyết
định rollback, vì chuyển traffic qua lại giữa hai region nhiều lần gây hại hơn là
đứng yên ở một trạng thái xấu.

Các bước rollback:

    python3 chaos/kill_region.py restore --region a --backend bare
    curl -s localhost:8001/readyz; echo

Chỉ khi region chính trả HTTP 200 mới chuyển traffic về:

    printf a > edge/active_region
    curl -s localhost:8080/v1/infer; echo

Dấu hiệu hoàn tất: response có edge_region là a và không có trường error.

Nếu region chính không trả 200, không được ghi vào edge/active_region. Giữ nguyên
traffic ở region phụ dù đang lỗi, và leo thang ngay cho trưởng nhóm SRE.

## Thang leo thang

| Tình huống | Liên hệ |
|---|---|
| Cả hai region cùng down | Trưởng nhóm SRE, ngay lập tức |
| Chưa từng có snapshot nào | Trưởng nhóm Data Platform |
| Cần quyết định rollback | Trưởng nhóm SRE trực |
| Dữ liệu mất vượt ngưỡng nghiệp vụ | Chủ sở hữu dữ liệu và trưởng nhóm SRE |