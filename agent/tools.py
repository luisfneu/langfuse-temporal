"""get_weather bate em API real (Open-Meteo, sem key). calculate é local (ast, sem eval)."""

import ast
import operator
from typing import Any

import requests

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_weather",
        "description": "Retorna o clima atual de uma cidade (dado real, via Open-Meteo).",
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

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_HTTP_TIMEOUT = 10

# WMO weather interpretation codes (tabela oficial do Open-Meteo)
_WEATHER_CODES = {
    0: "céu limpo", 1: "predominantemente limpo", 2: "parcialmente nublado", 3: "nublado",
    45: "névoa", 48: "névoa com geada",
    51: "garoa fraca", 53: "garoa moderada", 55: "garoa forte",
    61: "chuva fraca", 63: "chuva moderada", 65: "chuva forte",
    71: "neve fraca", 73: "neve moderada", 75: "neve forte",
    80: "pancadas de chuva fracas", 81: "pancadas de chuva moderadas", 82: "pancadas de chuva fortes",
    95: "trovoada", 96: "trovoada com granizo fraco", 99: "trovoada com granizo forte",
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
    # erro de rede/5xx propaga (deixa o RetryPolicy da activity tentar de
    # novo) — só "cidade não encontrada" é tratado aqui, não é erro transiente.
    geo = requests.get(
        _GEOCODE_URL,
        params={"name": city, "count": 1, "language": "pt", "format": "json"},
        timeout=_HTTP_TIMEOUT,
    )
    geo.raise_for_status()
    results = geo.json().get("results")
    if not results:
        return f"não encontrei a cidade '{city}'"

    place = results[0]
    forecast = requests.get(
        _FORECAST_URL,
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,weather_code",
            "timezone": "auto",
        },
        timeout=_HTTP_TIMEOUT,
    )
    forecast.raise_for_status()
    current = forecast.json()["current"]
    description = _WEATHER_CODES.get(current["weather_code"], "condição desconhecida")
    label = ", ".join(p for p in (place.get("name"), place.get("country")) if p)
    return f"{label}: {current['temperature_2m']}°C, {description}"


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
