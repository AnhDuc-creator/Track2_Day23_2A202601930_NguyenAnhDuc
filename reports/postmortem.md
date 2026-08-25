# Postmortem — Region A outage (drill 25/08/2026)

Sinh viên: Nguyễn Anh Đức — MSSV 2A202601930 — K4
Mức độ: SEV1, mô phỏng có kiểm soát.
Trạng thái: đã đóng, traffic phục vụ bởi Region B.

## Tóm tắt

Region A bị mất khả năng phản hồi lúc 13:39:21 UTC do network partition mô phỏng
bằng SIGSTOP. Toàn bộ traffic đi qua edge nhận ReadTimeout trong 28.2 giây trước khi
Region B tiếp quản. 14 trên 158 request thất bại. Hai document ingest trong khoảng
giữa hai chu kỳ replication bị mất vĩnh viễn.

RTO đo được 28.2 giây so với mục tiêu 300 giây. RPO đo được 4.0 giây tương đương
2 document.

## Timeline

| Thời điểm UTC | Kể từ mốc 0 | Diễn biến |
|---|---:|---|
| 13:39:21 | 0.0s | Region A ngừng phản hồi. Kết nối TCP vẫn mở nhưng không ai trả lời |
| 13:39:21 | 0.0s | Request đầu tiên của user timeout sau 2018.7ms |
| 13:39:36 | 14.9s | Health checker ghi nhận lần fail thứ ba liên tiếp, flag region-a UNHEALTHY |
| 13:39:39 | 17.2s | Runbook xác nhận outage dựa trên log health checker, thông báo incident |
| 13:39:39 | 17.3s | Restore snapshot vào Region B xong, ghi nhận RPO 4.0s và 2 document mất |
| 13:39:39 | 17.3s | Pool Region B chuyển từ warm sang full |
| 13:39:45 | 23.4s | Region B hết warm-up, trả /readyz 200 |
| 13:39:45 | 23.4s | Cutover, ghi "b" vào edge/active_region |
| 13:39:50 | 28.2s | Request thành công đầu tiên, phục vụ bởi Region B |

Nguồn: `chaos/chaos-events.jsonl:5`, `reports/health-events.jsonl:2`,
`reports/failover-events.jsonl:2`, `reports/drill-2-withdr.jsonl:39`,
`reports/runbook-run.jsonl:1`.

## Nguyên nhân gốc

Nguyên nhân trực tiếp là network partition được tạo ra có chủ đích. Điều đáng phân
tích không phải bản thân sự cố mà là hệ thống mất bao lâu để tự đứng dậy, và thời
gian đó đi đâu.

## Gap analysis

Bốn khoảng thời gian tạo nên RTO, xếp theo mức độ đóng góp.

**Gap 1: detection floor 14.9 giây, chiếm 53 phần trăm RTO.**
interval 5 giây nhân threshold 3 cho ra sàn 15 giây. Trong 15 giây đó user đã nhận
lỗi mà chưa ai biết. Giảm interval xuống 2 giây sẽ kéo sàn còn 6 giây, nhưng đổi lại
là tăng rủi ro flapping: một lần chậm mạng thoáng qua có thể kích hoạt failover không
cần thiết, và failover hai chiều liên tục còn tệ hơn một outage duy nhất. Giảm
threshold xuống 2 thì mất luôn khả năng phân biệt nhiễu với sự cố thật.

**Gap 2: GPU pool warm-up 6.1 giây.**
Ghi full vào pool_state không làm Region B phục vụ được ngay. Đây là chi phí cố hữu
của mô hình active-passive: region phụ tiết kiệm tài nguyên bằng cách không giữ pool
ở trạng thái sẵn sàng, và trả giá bằng thời gian warm-up lúc cần. Muốn xoá gap này
phải chuyển sang active-active, tức là trả tiền cho hai pool chạy song song suốt.

**Gap 3: DNS TTL cache 4.8 giây.**
edge/proxy.py cache kết quả resolve trong 5 giây, nên ngay cả sau khi cutover, một số
request vẫn tiếp tục đi về Region A đã chết. Trong hệ thống thật con số này thường tệ
hơn nhiều vì resolver trung gian không tôn trọng TTL. Giảm TTL làm tăng tải lên lớp
resolve và không loại bỏ được hoàn toàn vấn đề.

**Gap 4: restore snapshot và quy trình xác nhận 2.4 giây.**
Đây là phần nhỏ nhất và cũng ít đáng tối ưu nhất. Với dataset lớn hơn nhiều lần, con
số này sẽ tăng tuyến tính theo kích thước index và trở thành nút cổ chai.

**Gap về dữ liệu:** replication chạy mỗi 30 giây trong khi ingest chạy liên tục
0.5 doc mỗi giây. Bất kỳ document nào ghi vào giữa hai chu kỳ đều có nguy cơ mất.
Lần drill này may mắn rơi gần cuối chu kỳ nên chỉ mất 2 document. Trường hợp xấu nhất
là mất tới 15 document.

**Gap về quy trình:** Region B khởi đầu hoàn toàn rỗng, không có dữ liệu và không có
model weights. Khoảng cách này không được phát hiện bởi bất kỳ cảnh báo nào cho tới
khi outage thật xảy ra. Một region phụ chưa từng được kiểm tra khả năng phục vụ thì
không khác gì không có region phụ.

## Điều gì đã hoạt động tốt

Failover tuân thủ đúng thứ tự năm bước và không cutover trước khi Region B chứng minh
được là sẵn sàng. Nếu đổi DNS ngay sau bước scale pool, user sẽ nhận 503 từ cả hai
region trong suốt 6 giây warm-up, và RTO sẽ dài hơn chứ không ngắn hơn.

Health checker dùng /readyz thay vì /healthz nên phân biệt được "process còn sống"
với "region phục vụ được". Region B suốt giai đoạn đầu drill có process hoàn toàn bình
thường nhưng bị flag UNHEALTHY đúng như thực tế, xem `reports/health-events.jsonl:1`.

Runbook chờ health checker xác nhận rồi mới hành động, thay vì tự quyết định. Nhờ vậy
con số RTO đo được phản ánh phản ứng của automation chứ không phải tốc độ gõ phím của
người vận hành.

## Action item

| Việc | Ưu tiên | Chủ sở hữu | Tiêu chí hoàn thành |
|---|---|---|---|
| Giảm chu kỳ replication từ 30s xuống 10s, đo lại RPO trung bình qua 5 lần drill | Cao | Data Platform | RPO trung bình dưới 10s, độ lệch chuẩn có ghi lại |
| Thêm cảnh báo khi Region B có vector count bằng 0 hoặc thiếu model weights | Cao | SRE | Alert kích hoạt trong vòng 1 phút kể từ khi Region B mất dữ liệu |
| Chạy game day hàng tháng theo đúng kịch bản này, ghi lại RTO mỗi lần | Cao | SRE | Có bảng RTO theo tháng, phát hiện được xu hướng xấu đi |
| Đánh giá chuyển sang active-active để xoá 6.1s warm-up, kèm ước tính chi phí | Trung bình | Kiến trúc | Có tài liệu so sánh chi phí và RTO của hai phương án |
| Giảm EDGE_TTL_SECONDS từ 5s xuống 2s, đo tác động lên tải resolve | Trung bình | Platform | Gap DNS dưới 2.5s, tải resolve tăng dưới 20 phần trăm |
| Thử nghiệm interval 3s với threshold 3, kiểm tra tỷ lệ false positive | Thấp | SRE | Detection floor 9s, không có failover giả trong 100 chu kỳ |

## Câu trả lời cho câu hỏi trong docstring health_checker

Với interval 5 giây và threshold 3, thời điểm sớm nhất phát hiện được outage là 15
giây. Con số này nằm trong RTO, không phải chi phí ẩn bên ngoài.

Nếu mục tiêu RTO là 300 giây, ngân sách còn lại sau khi trừ restore, warm-up và DNS
TTL vẫn rất rộng, nên về lý thuyết interval có thể lên tới hàng chục giây mà vẫn đạt
mục tiêu. Nhưng interval lớn nghĩa là user chịu lỗi lâu hơn trước khi có ai biết, nên
lựa chọn thực tế không nên chỉ dựa vào việc lọt dưới ngưỡng mục tiêu.