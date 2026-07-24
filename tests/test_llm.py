import httpx

from song_agent.llm import api_error_message


def test_api_error_message_supports_openai_shape() -> None:
    response = httpx.Response(
        400,
        json={"error": {"message": "invalid model"}},
    )

    assert api_error_message(response) == "invalid model"


def test_api_error_message_supports_siliconflow_shape() -> None:
    response = httpx.Response(
        400,
        json={"code": 20012, "message": "Model does not exist", "data": None},
    )

    assert api_error_message(response) == "Model does not exist (code=20012)"


def test_api_error_message_ignores_unstructured_body() -> None:
    response = httpx.Response(502, text="<html>bad gateway</html>")

    assert api_error_message(response) is None
