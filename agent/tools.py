"""Tools dummy só pra exercitar o loop de tool-use. Sem chamada externa real."""

import ast
import operator
from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_weather",
        "description": "Retorna o clima atual (dado fake, só pra teste) de uma cidade.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "Nome da cidade"},
            },
            "required": ["city"],
        },
    },
    {
        "name": "calculate",
        "description": "Avalia uma expressão aritmética simples (+ - * / parênteses, números).",
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Ex: (12 + 8) * 3",
                },
            },
            "required": ["expression"],
        },
    },
]

_FAKE_WEATHER = {
    "lisboa": "21°C, ensolarado",
    "sao paulo": "19°C, nublado",
    "são paulo": "19°C, nublado",
    "rio de janeiro": "28°C, ensolarado",
}

_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"expressão não suportada: {ast.dump(node)}")


def get_weather(city: str) -> str:
    key = city.strip().lower()
    return _FAKE_WEATHER.get(key, f"{city}: 24°C, parcialmente nublado (dado fake)")


def calculate(expression: str) -> str:
    try:
        tree = ast.parse(expression, mode="eval")
        return str(_safe_eval(tree.body))
    except Exception as exc:  # expressão inválida é esperado, não é bug
        return f"erro ao calcular '{expression}': {exc}"


def execute_tool_call(name: str, tool_input: dict[str, Any]) -> str:
    if name == "get_weather":
        return get_weather(tool_input["city"])
    if name == "calculate":
        return calculate(tool_input["expression"])
    return f"tool desconhecida: {name}"
