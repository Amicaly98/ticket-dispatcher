"""离线解析器测试：用已知 stdout 文本断言解析正确。

覆盖主要模式 + 边界情况，无需二进制，可在 CI 跑。
"""
import pytest
from src.driver._parse import parse_target_output
from src.models import Buyer, Job


def _job(buyer_ids=None):
    return Job.build(
        job_id="job-parse-1", task_id="t1", worker_id="w1", program="test",
        project_id=1, screen_id=1, sku_id=1, count=1, pay_money=100, deadline=0,
        account={"uid": 1, "cookie": {}},
        buyers=[Buyer(id=bid, name=f"b{bid}") for bid in (buyer_ids or [100])])


class TestPattern1_JsonOrderId:
    def test_order_id_in_json(self):
        stdout = 'some log line\n{"orderId": "ORDER_123456", "errno": 0}\nmore logs'
        r = parse_target_output(stdout, 0, _job())
        assert r.success is True
        assert r.order_id == "ORDER_123456"
        assert r.reason == "ok"

    def test_order_id_nested_data(self):
        stdout = '{"code": 0, "data": {"orderId": "NESTED_789"}}'
        r = parse_target_output(stdout, 0, _job())
        assert r.success is True
        assert r.order_id == "NESTED_789"

    def test_order_id_with_surrounding_text(self):
        stdout = '[INFO] 下单响应: {"orderId": "ORD_ABC", "errno": 0} (耗时 1.2s)'
        r = parse_target_output(stdout, 0, _job())
        assert r.success is True
        assert r.order_id == "ORD_ABC"


class TestPattern2_Errno:
    def test_errno_zero(self):
        stdout = '请求完成\n{"errno": 0, "errmsg": "success"}\n'
        r = parse_target_output(stdout, 0, _job())
        assert r.success is True
        assert r.reason == "ok"

    def test_errno_nonzero(self):
        stdout = '{"errno": -401, "errmsg": "未登录"}'
        r = parse_target_output(stdout, 0, _job())
        assert r.success is False


class TestPattern3_Keywords:
    def test_order_create_success(self):
        stdout = 'order_create_success orderId=XYZ'
        r = parse_target_output(stdout, 0, _job())
        assert r.success is True

    def test_order_success_chinese(self):
        stdout = '下单成功！'
        r = parse_target_output(stdout, 0, _job())
        assert r.success is True

    def test_order_created(self):
        stdout = '订单创建成功'
        r = parse_target_output(stdout, 0, _job())
        assert r.success is True


class TestPattern4_ExitCode:
    def test_exit_0_no_result(self):
        stdout = '程序正常退出，但没有找到明确结果'
        r = parse_target_output(stdout, 0, _job())
        assert r.success is False
        assert r.reason == "no_result"

    def test_exit_nonzero(self):
        stdout = 'some error output'
        r = parse_target_output(stdout, 1, _job())
        assert r.success is False
        assert r.reason == "exit_1"

    def test_exit_137(self):
        stdout = ''
        r = parse_target_output(stdout, 137, _job())
        assert r.reason == "exit_137"


class TestEdgeCases:
    def test_empty_stdout(self):
        r = parse_target_output("", 0, _job())
        assert r.success is False
        assert r.reason == "no_result"

    def test_buyers_secured_on_success(self):
        stdout = '{"orderId": "ORD_1", "errno": 0}'
        r = parse_target_output(stdout, 0, _job(buyer_ids=[101, 102]))
        assert r.success is True
        assert r.buyers_secured == [101, 102]
        assert r.buyers_attempted == [101, 102]

    def test_buyers_empty_on_failure(self):
        stdout = '{"errno": -1}'
        r = parse_target_output(stdout, 0, _job(buyer_ids=[101]))
        assert r.success is False
        assert r.buyers_secured == []

    def test_extra_reason_override(self):
        stdout = 'some output'
        r = parse_target_output(stdout, 0, _job(), extra_reason="bypass_failed")
        assert r.reason == "bypass_failed"

    def test_order_id_takes_priority_over_errno(self):
        """orderId 模式优先于 errno 模式（两者都匹配时）。"""
        stdout = '{"orderId": "WINNER", "errno": 0}\n{"errno": -1}'
        r = parse_target_output(stdout, 0, _job())
        assert r.success is True
        assert r.order_id == "WINNER"

    def test_order_id_in_text(self):
        """纯文本订单号提取。"""
        stdout = '订单号：8001350956778121\n操作完成'
        r = parse_target_output(stdout, 0, _job())
        assert r.success is True
        assert r.order_id == "8001350956778121"


class TestKeywordClassification:
    """已知错误关键字 → 友好 reason 短码。"""

    def test_not_logged_in(self):
        r = parse_target_output("错误：未登录或登录失效", 1, _job())
        assert r.reason == "not_logged_in"

    def test_sold_out_keyword(self):
        r = parse_target_output("很遗憾，库存不足", 0, _job())
        assert r.reason == "sold_out"

    def test_keyword_overrides_no_result(self):
        """exit=0 本会判 no_result，但命中关键字时用友好短码。"""
        r = parse_target_output("项目不可售，未开售", 0, _job())
        assert r.reason == "not_on_sale"

    def test_unknown_keyword_falls_back_to_exit(self):
        r = parse_target_output("一些无法识别的输出", 3, _job())
        assert r.reason == "exit_3"


class TestDetailTail:
    """失败结果带 detail 诊断尾巴；成功不带。"""

    def test_failure_carries_detail(self):
        stdout = "\n".join(f"line {i}" for i in range(50)) + "\n库存不足"
        r = parse_target_output(stdout, 1, _job())
        assert r.success is False
        assert r.detail, "失败应带 detail"
        assert "库存不足" in r.detail
        assert "(exit=1)" in r.detail
        # 只留最后 30 行 → line 0 不应出现
        assert "line 0\n" not in r.detail

    def test_success_no_detail(self):
        r = parse_target_output('{"orderId": "OK_1", "errno": 0}', 0, _job())
        assert r.success is True
        assert r.detail == ""

    def test_detail_char_capped(self):
        stdout = "x" * 10000
        r = parse_target_output(stdout, 1, _job())
        assert len(r.detail) <= 4096 + 32   # 上限 + exit 后缀余量
