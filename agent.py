import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from status_tools import SCHEMAS
"""Bài tập chính: hiện chưa có lời giải. Xem GUIDE.md."""

SYSTEM_PROMPT = '''Bạn là trợ lý điều tra trạng thái GitHub và Cloudflare.
Dùng tools trước khi khẳng định về trạng thái. Phân biệt incident hiện tại và lịch sử,
phạm vi component/địa lý/khách hàng, thời gian snapshot và giới hạn bằng chứng.
Đừng khẳng định một incident là nguyên nhân lỗi của người dùng khi chưa đủ bằng chứng.
Nếu người dùng yêu cầu danh sách đầy đủ cho một khoảng thời gian mà tool không bao phủ,
hãy trả lời rằng không thể xác minh danh sách đầy đủ từ dữ liệu hiện có.
Không nói "không có incident" chỉ vì không tìm thấy trong 50 incident gần nhất.
Không liệt kê incident ngoài khoảng thời gian được hỏi như thể chúng trả lời câu hỏi.
Đọc chi tiết các incident gần đây không thể chứng minh lịch sử của cả một năm.
'''


def retry_after_seconds(tool_result):
    raw = tool_result.get("retry_after_seconds")
    if raw is None:
        raw = tool_result.get("retry_after")

    if raw is None:
        return 1.0  # Không có chỉ dẫn: chờ ngắn trước lần sau.

    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            retry_time = parsedate_to_datetime(str(raw))
            if retry_time.tzinfo is None:
                retry_time = retry_time.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                (retry_time - datetime.now(timezone.utc)).total_seconds(),
            )
        except (TypeError, ValueError, OverflowError):
            return None


def run_agent(question, provider, tools, max_steps=6):
    schemas = getattr(tools, "schemas", SCHEMAS)
    system_prompt = getattr(tools, "system_prompt", SYSTEM_PROMPT)
    if hasattr(tools, "schemas"):
        system_prompt += (
            "\nKhi tool báo lỗi, đọc thông báo rồi điều chỉnh lời gọi. "
            "Nếu tham số sai định dạng, dùng đúng kiểu trong schema. "
            "Chỉ thử lại một tool tối đa 3 lần; nếu vẫn không lấy được dữ liệu, "
            "báo rõ provider nào chưa xác minh được. "
            "Nêu nguồn và captured_at riêng cho mỗi provider. "
            "Khi cần đọc tiếp timeline, dùng next_offset."
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]
    trace = []
    usage = []
    source_urls = set()
    asks_for_full_history = bool(
        re.search(r"\b(?:19|20)\d{2}\b", question)
        and re.search(r"đầy đủ|tất cả|toàn bộ|không sót|complete|all incidents", question, re.I)
    )
    limited_status_sources = []
    rate_limit_count = {}
    retry_wait = {}
    blocked_tools = set()
    status_sources = {}
    for step in range(1, max_steps + 1):
        try:
            result = provider.complete(messages, schemas)
        except Exception as exc:
            trace.append({"event": "provider_error", "step": step, "error": str(exc)})
            return {
                "status": "error",
                "answer": "",
                "trace": trace,
                "usage": usage,
                "error": str(exc),
            }

        usage.append(result.get("usage", {}))
        messages.append(result["message"])
        calls = result.get("calls", [])
        finish = result.get("finish")

        trace.append({"event": "model", "step": step, "finish": finish})

        if finish == "length":
            trace.append({"event": "model_error", "step": step, "error": "Output bị cắt"})
            return {
                "status": "error",
                "answer": "",
                "trace": trace,
                "usage": usage,
                "error": "Model output bị cắt (finish=length)",
            }

        if calls:
            for call in calls:
                try:
                    args = json.loads(call["arguments"])
                    if not isinstance(args, dict):
                        raise ValueError("Arguments phải là JSON object")
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    tool_result = {
                        "ok": False,
                        "error": "INVALID_ARGUMENTS",
                        "message": str(exc),
                    }
                else:
                    try:
                        key = (call["name"], args.get("provider"))

                        if key in blocked_tools or rate_limit_count.get(key, 0) >= 3:
                            tool_result = {
                                "ok": False,
                                "error": "RATE_LIMITED",
                                "retriable": False,
                                "message": "Đã dừng thử; không thể xác minh dữ liệu này.",
                            }
                        else:
                            wait_seconds = retry_wait.pop(key, 0)
                            if wait_seconds:
                                time.sleep(wait_seconds)

                            tool_result = tools.execute(call["name"], args)

                            if (
                                isinstance(tool_result, dict)
                                and tool_result.get("error") == "RATE_LIMITED"
                            ):
                                rate_limit_count[key] = rate_limit_count.get(key, 0) + 1
                                delay = retry_after_seconds(tool_result)

                                if rate_limit_count[key] >= 3:
                                    tool_result["retriable"] = False
                                    tool_result["message"] += " Đã đạt giới hạn 3 lần."
                                elif delay is None or delay > 2:
                                    blocked_tools.add(key)
                                    tool_result["retriable"] = False
                                    tool_result["message"] += (
                                        " Thời gian chờ không phù hợp với budget; dừng thử."
                                    )
                                else:
                                    retry_wait[key] = delay

                    except Exception as exc:
                        tool_result = {
                            "ok": False,
                            "error": type(exc).__name__,
                            "message": str(exc),
                        }
                if (
                    call["name"] == "get_incident_details"
                    and isinstance(tool_result, dict)
                    and tool_result.get("ok") is True
                    and tool_result.get("url")
                ):
                    source_urls.add(tool_result["url"])
                if (
                    call["name"] == "get_status"
                    and isinstance(tool_result, dict)
                    and "50 incident" in tool_result.get("note", "")
                ):
                    limited_status_sources.append(tool_result)
                if (
                    isinstance(tool_result, dict)
                    and tool_result.get("provider")
                    and tool_result.get("source")
                    and tool_result.get("captured_at")
                ):
                    status_sources[tool_result["provider"]] = tool_result
                trace.append({
                    "event": "tool",
                    "step": step,
                    "name": call["name"],
                    "arguments": call["arguments"],
                    "result": tool_result,
                })
                observation = dict(tool_result)
                observation.pop("distractors", None)
                observation.pop("external_notice", None)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(observation, ensure_ascii=False),
                })

            continue

        if finish == "stop":
            answer = result["message"].get("content") or ""

            if asks_for_full_history and limited_status_sources:
                years = sorted(set(re.findall(r"\b(?:19|20)\d{2}\b", question)))
                period = "–".join(years) if len(years) > 1 else f"năm {years[0]}"
                sources = []
                for item in limited_status_sources:
                    source = f"{item['provider']} (snapshot {item['captured_at']}): {item['source']}"
                    if source not in sources:
                        sources.append(source)
                answer = (
                    f"Tôi không thể xác minh danh sách đầy đủ các incident trong {period} "
                    "từ dữ liệu hiện có. Công cụ chỉ tìm trong 50 incident gần nhất "
                    "và chỉ hiển thị tối đa 5 mục khớp mỗi lần gọi. Vì vậy, không thể "
                    "xác định số lượng incident của giai đoạn đó hoặc bảo đảm không bỏ sót. "
                    "Nguồn đã kiểm tra: " + "; ".join(sources) + "."
                )
                if source_urls:
                    answer += (
                        " Một URL incident riêng lẻ đã quan sát: "
                        + sorted(source_urls)[0]
                        + "; URL này không chứng minh danh sách lịch sử đã đầy đủ."
                    )
                trace.append({
                    "event": "validation_incomplete_history",
                    "step": step,
                    "providers": sorted({item["provider"] for item in limited_status_sources}),
                })
                return {
                    "status": "completed",
                    "answer": answer,
                    "trace": trace,
                    "usage": usage,
                }

            if source_urls and not any(url in answer for url in source_urls):
                trace.append({
                    "event": "validation_missing_source",
                    "step": step,
                    "urls": sorted(source_urls),
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "Hãy sửa câu trả lời vừa rồi để dẫn URL incident "
                        "đã quan sát từ tool: " + ", ".join(sorted(source_urls))
                    ),
                })
                continue

            missing_sources = [
                item for item in status_sources.values()
                if item["source"] not in answer
                or item["captured_at"] not in answer
            ]

            if missing_sources:
                answer += "\nNguồn và thời điểm dữ liệu: " + "; ".join(
                    f"{item['provider']} ({item['captured_at']}): {item['source']}"
                    for item in missing_sources
                )
            return {
                "status": "completed",
                "answer": answer,
                "trace": trace,
                "usage": usage,
            }


    trace.append({"event": "max_steps", "step": max_steps})
    return {
        "status": "max_steps",
        "answer": "",
        "trace": trace,
        "usage": usage,
    }
