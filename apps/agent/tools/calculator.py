"""
calculator 工具 - 精确数学计算
使用 Python ast 模块做安全沙箱，白名单节点遍历，禁止 import/exec/属性访问/函数调用
（仅允许内置数学函数），防止代码注入。
"""
import ast
import math
import operator
from typing import Any, Dict

from loguru import logger

from .base import BaseTool, ToolContext


# 允许的二元运算符（映射 ast 节点 → 运算函数）
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# 允许的一元运算符
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# 允许的内置数学函数（白名单，防注入）
_SAFE_FUNCS = {
    'abs': abs,
    'round': round,
    'min': min,
    'max': max,
    'sum': sum,
    'sqrt': math.sqrt,
    'log': math.log,
    'log10': math.log10,
    'log2': math.log2,
    'exp': math.exp,
    'sin': math.sin,
    'cos': math.cos,
    'tan': math.tan,
    'asin': math.asin,
    'acos': math.acos,
    'atan': math.atan,
    'ceil': math.ceil,
    'floor': math.floor,
    'pow': pow,
}

# 允许的常量
_SAFE_CONSTS = {
    'pi': math.pi,
    'e': math.e,
    'tau': math.tau,
    'inf': math.inf,
    'nan': math.nan,
}


class CalculatorTool(BaseTool):
    """精确数学计算工具

    当用户问题涉及数值计算（如百分比、统计、单位换算、财务计算等）时调用。
    使用安全沙箱解析数学表达式，避免 LLM 直接心算导致的精度错误。
    """

    name = 'calculator'
    description = (
        '执行精确的数学表达式计算。支持四则运算、幂运算、对数、三角函数、'
        '统计函数（min/max/sum/avg）等。当需要精确数值结果时调用，避免估算误差。'
    )
    parameters = {
        'type': 'object',
        'properties': {
            'expression': {
                'type': 'string',
                'description': (
                    'Python 风格的数学表达式，如 "3.14 * 2 ** 2" 或 '
                    '"sqrt(144) + log(100, 10)" 或 "sum([1,2,3,4,5]) / 5"'
                ),
            },
        },
        'required': ['expression'],
    }

    def execute(self, ctx: ToolContext, expression: str, **kwargs) -> Dict[str, Any]:
        """安全执行数学表达式

        使用 ast.parse 解析表达式为 AST，遍历节点时仅允许白名单内的
        运算符/函数/常量，任何非法节点（如属性访问、import、函数定义）
        都会抛出 ValueError，确保无法执行任意代码。

        Args:
            ctx: 执行上下文（本工具不使用）
            expression: 数学表达式字符串

        Returns:
            {'result': str, 'ok': bool, 'meta': {'value': Any}}
        """
        if not expression or not isinstance(expression, str):
            return {'result': '表达式不能为空', 'ok': False, 'meta': {}}

        expr = expression.strip()
        # 长度限制，防止超长表达式攻击
        if len(expr) > 500:
            return {'result': '表达式过长（>500 字符）', 'ok': False, 'meta': {}}

        try:
            # 解析为 AST，mode='eval' 限制只能解析单个表达式（不能是语句）
            tree = ast.parse(expr, mode='eval')
            value = self._eval_node(tree.body)
            # 格式化结果：浮点数保留合理精度，整数直接显示
            if isinstance(value, float):
                # 整数浮点（如 4.0）显示为 4，避免 LLM 困惑
                if value.is_integer():
                    result_str = str(int(value))
                else:
                    result_str = f'{value:.10g}'
            else:
                result_str = str(value)
            return {
                'result': f'{expression} = {result_str}',
                'ok': True,
                'meta': {'value': value, 'expression': expression},
            }
        except Exception as e:
            logger.info(f'[CalculatorTool] eval failed: {e} | expr: {expr[:100]}')
            return {
                'result': f'计算失败: {e.__class__.__name__}: {str(e)[:200]}',
                'ok': False,
                'meta': {'expression': expression},
            }

    def _eval_node(self, node) -> Any:
        """递归求值 AST 节点，白名单校验

        遇到任何不在白名单内的节点类型立即抛错，确保无法执行危险操作。
        """
        # 数字字面量
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError(f'不支持的字面量类型: {type(node.value).__name__}')
        # 变量名（只能是白名单常量）
        if isinstance(node, ast.Name):
            if node.id in _SAFE_CONSTS:
                return _SAFE_CONSTS[node.id]
            raise ValueError(f'未定义的变量: {node.id}')
        # 二元运算
        if isinstance(node, ast.BinOp):
            op_func = _BIN_OPS.get(type(node.op))
            if not op_func:
                raise ValueError(f'不支持的运算符: {type(node.op).__name__}')
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            return op_func(left, right)
        # 一元运算
        if isinstance(node, ast.UnaryOp):
            op_func = _UNARY_OPS.get(type(node.op))
            if not op_func:
                raise ValueError(f'不支持的一元运算符: {type(node.op).__name__}')
            operand = self._eval_node(node.operand)
            return op_func(operand)
        # 函数调用（仅白名单函数）
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError('仅支持直接函数调用，不支持属性访问')
            func_name = node.func.id
            if func_name not in _SAFE_FUNCS:
                raise ValueError(f'不支持的函数: {func_name}')
            # 关键字参数禁止（简化安全模型）
            if node.keywords:
                raise ValueError('不支持关键字参数')
            args = [self._eval_node(a) for a in node.args]
            return _SAFE_FUNCS[func_name](*args)
        # 列表字面量（用于 sum/min/max 等）
        if isinstance(node, ast.List):
            return [self._eval_node(e) for e in node.elts]
        raise ValueError(f'不支持的语法节点: {type(node).__name__}')
