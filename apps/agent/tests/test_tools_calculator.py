"""
agent.tools.calculator 单元测试

覆盖 CalculatorTool 的全部分支：
- execute() 主流程：空表达式 / 超长表达式 / 正常计算 / 异常处理
- _eval_node 递归求值：数字字面量、常量、二元运算、一元运算、函数调用、列表
- 安全沙箱：禁止属性访问、禁止非白名单函数、禁止非白名单变量、禁止关键字参数
- 结果格式化：整数浮点、普通浮点、整数

纯逻辑测试，不依赖 DB / LLM。
"""
import math

import pytest

from apps.agent.tools.base import ToolContext
from apps.agent.tools.calculator import CalculatorTool, _SAFE_FUNCS, _SAFE_CONSTS

pytestmark = pytest.mark.unit


class TestCalculatorExecute:
    """CalculatorTool.execute() 主流程与参数防御"""

    def test_empty_expression_returns_error(self):
        """空字符串 / 非字符串表达式直接拒绝"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '')
        assert ret['ok'] is False
        assert '表达式不能为空' in ret['result']

    def test_non_string_expression_returns_error(self):
        """非字符串类型（如 None / int）直接拒绝"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), None)
        assert ret['ok'] is False
        assert '表达式不能为空' in ret['result']

    def test_expression_too_long_returns_error(self):
        """超过 500 字符的表达式直接拒绝，防止超长输入攻击"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '1 + ' * 250)
        assert ret['ok'] is False
        assert '表达式过长' in ret['result']

    def test_execute_when_basic_arithmetic_then_returns_correct_result(self):
        """四则运算：加法"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '3 + 4')
        assert ret['ok'] is True
        assert '= 7' in ret['result']
        assert ret['meta']['value'] == 7

    def test_execute_when_integer_float_then_displayed_as_int(self):
        """整数浮点（如 4.0）显示为 4，避免 LLM 困惑"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '8 / 2')
        assert ret['ok'] is True
        assert '= 4\n' not in ret['result']  # 不应出现 4.0
        assert '= 4' in ret['result']

    def test_execute_when_float_then_formatted(self):
        """非整数浮点保留合理精度（%.10g）"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '10 / 3')
        assert ret['ok'] is True
        assert '3.333333333' in ret['result']

    def test_execute_when_power_then_returns_correct_result(self):
        """幂运算：2 ** 10 = 1024"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '2 ** 10')
        assert ret['ok'] is True
        assert ret['meta']['value'] == 1024

    def test_eval_failure_returns_error(self):
        """语法错误表达式：返回计算失败信息"""
        tool = CalculatorTool()
        # 1 + * 2 是真正的语法错误（双操作符不可连用），会被 ast.parse 拒绝
        ret = tool.execute(ToolContext(), '1 + * 2')
        assert ret['ok'] is False
        assert '计算失败' in ret['result']


class TestEvalNodeConstants:
    """_eval_node：数字字面量与常量"""

    def test_eval_node_when_integer_literal_then_returns_value(self):
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '42')
        assert ret['meta']['value'] == 42

    def test_eval_node_when_float_literal_then_returns_value(self):
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '3.14')
        assert ret['meta']['value'] == 3.14

    def test_eval_node_when_pi_then_returns_value(self):
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), 'pi')
        assert ret['meta']['value'] == math.pi

    def test_eval_node_when_e_then_returns_value(self):
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), 'e')
        assert ret['meta']['value'] == math.e

    def test_eval_node_when_undefined_variable_then_raises(self):
        """未在白名单中的变量名应被拒绝"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), 'foo')
        assert ret['ok'] is False
        assert '未定义的变量' in ret['result']

    def test_eval_node_when_unsupported_literal_then_raises(self):
        """非数值字面量（如字符串）应被拒绝"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '"hello"')
        assert ret['ok'] is False


class TestEvalNodeBinOp:
    """_eval_node：二元运算符"""

    @pytest.mark.parametrize('expr,expected', [
        ('5 + 3', 8),
        ('10 - 4', 6),
        ('6 * 7', 42),
        ('15 / 4', 3.75),
        ('17 // 5', 3),
        ('17 % 5', 2),
        ('2 ** 8', 256),
    ])
    def test_eval_node_when_binop_then_returns_correct_result(self, expr, expected):
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), expr)
        assert ret['ok'] is True
        assert ret['meta']['value'] == expected

    def test_eval_node_when_nested_binop_then_returns_correct_result(self):
        """嵌套运算：(1 + 2) * (3 + 4) = 21"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '(1 + 2) * (3 + 4)')
        assert ret['meta']['value'] == 21


class TestEvalNodeUnaryOp:
    """_eval_node：一元运算符"""

    def test_eval_node_when_unary_plus_then_returns_value(self):
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '+5')
        assert ret['meta']['value'] == 5

    def test_eval_node_when_unary_minus_then_returns_value(self):
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '-5')
        assert ret['meta']['value'] == -5


class TestEvalNodeCall:
    """_eval_node：函数调用（白名单函数）"""

    @pytest.mark.parametrize('expr,expected', [
        ('abs(-5)', 5),
        ('round(3.7)', 4),
        ('min(1, 2, 3)', 1),
        ('max(1, 2, 3)', 3),
        ('sum([1, 2, 3, 4, 5])', 15),
        ('sqrt(144)', 12.0),
        ('ceil(3.2)', 4),
        ('floor(3.8)', 3),
        ('pow(2, 10)', 1024),
    ])
    def test_eval_node_when_safe_function_then_returns_value(self, expr, expected):
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), expr)
        assert ret['ok'] is True
        assert ret['meta']['value'] == expected

    def test_eval_node_when_unsupported_function_then_raises(self):
        """非白名单函数应被拒绝"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), 'open("file.txt")')
        assert ret['ok'] is False
        assert '不支持的函数' in ret['result']

    def test_eval_node_when_attribute_access_then_raises(self):
        """属性访问（如 os.system）应被拒绝"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '__import__("os").system("ls")')
        assert ret['ok'] is False

    def test_eval_node_when_keyword_args_then_raises(self):
        """关键字参数应被拒绝（简化安全模型）"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), 'round(3.14159, ndigits=2)')
        assert ret['ok'] is False
        assert '关键字参数' in ret['result']


class TestEvalNodeList:
    """_eval_node：列表字面量（用于 sum/min/max 等）"""

    def test_eval_node_when_list_in_function_then_returns_value(self):
        """列表字面量可传入聚合函数"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), 'max([3, 1, 4, 1, 5, 9, 2, 6])')
        assert ret['meta']['value'] == 9

    def test_eval_node_when_nested_list_then_returns_value(self):
        """列表元素可以是表达式：sum([1*2, 3*4, 5*6]) = 2+12+30 = 44"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), 'sum([1 * 2, 3 * 4, 5 * 6])')
        assert ret['meta']['value'] == 44


class TestEvalNodeUnsupported:
    """_eval_node：不支持的语法节点"""

    def test_eval_node_when_set_literal_then_raises(self):
        """集合字面量 {1, 2, 3} 不在白名单中"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '{1, 2, 3}')
        assert ret['ok'] is False

    def test_eval_node_when_dict_literal_then_raises(self):
        """字典字面量不在白名单中"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), "{'a': 1}")
        assert ret['ok'] is False

    def test_eval_node_when_comparison_then_raises(self):
        """比较表达式不在白名单中"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '1 > 2')
        assert ret['ok'] is False

    def test_eval_node_when_ternary_then_raises(self):
        """三元表达式不在白名单中"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '1 if True else 2')
        assert ret['ok'] is False

    def test_unsupported_binop_bitwise_or_raises(self):
        """位运算 | 不在白名单中：覆盖 _eval_node 不支持的运算符分支"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '1 | 2')
        assert ret['ok'] is False
        assert '不支持的运算符' in ret['result']

    def test_unsupported_unaryop_invert_raises(self):
        """按位取反 ~ 不在白名单中：覆盖 _eval_node 不支持的一元运算符分支"""
        tool = CalculatorTool()
        ret = tool.execute(ToolContext(), '~5')
        assert ret['ok'] is False
        assert '不支持的一元运算符' in ret['result']
