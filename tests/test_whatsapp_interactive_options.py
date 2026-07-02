from app.integrations.whatsapp.client import send_whatsapp_message
from app.integrations.whatsapp.parser import parse_whatsapp_payload


def _options_body(count: int) -> str:
    lines = ["Escolha uma opção:"]
    lines.extend(f"{index}. Opção {index}" for index in range(1, count + 1))
    return "\n".join(lines)


def test_whatsapp_message_with_two_options_uses_buttons():
    result = send_whatsapp_message("351900000000", _options_body(2), force_mock=True)

    assert result["type"] == "interactive"
    assert result["interactive_type"] == "button"
    assert result["body"] == "Escolha uma opção:"
    assert result["buttons"] == [
        {"id": "option_1", "title": "Opção 1"},
        {"id": "option_2", "title": "Opção 2"},
    ]


def test_whatsapp_message_with_three_options_uses_buttons():
    result = send_whatsapp_message("351900000000", _options_body(3), force_mock=True)

    assert result["interactive_type"] == "button"
    assert [button["id"] for button in result["buttons"]] == ["option_1", "option_2", "option_3"]


def test_whatsapp_message_with_four_options_uses_list():
    result = send_whatsapp_message("351900000000", _options_body(4), force_mock=True)

    assert result["type"] == "interactive"
    assert result["interactive_type"] == "list"
    assert result["body"] == "Escolha uma opção:"
    assert [row["id"] for row in result["list_rows"]] == [
        "option_1",
        "option_2",
        "option_3",
        "option_4",
    ]


def test_whatsapp_message_with_five_options_uses_list():
    result = send_whatsapp_message("351900000000", _options_body(5), force_mock=True)

    assert result["interactive_type"] == "list"
    assert result["list_rows"][4] == {"id": "option_5", "title": "Opção 5"}


def test_whatsapp_message_with_six_options_uses_text():
    body = _options_body(6)

    result = send_whatsapp_message("351900000000", body, force_mock=True)

    assert result["status"] == "mocked"
    assert result["body"] == body
    assert "type" not in result
    assert "buttons" not in result
    assert "list_rows" not in result


def test_parser_converts_button_option_id_to_number():
    messages = parse_whatsapp_payload(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "351900000000",
                                        "id": "m1",
                                        "type": "interactive",
                                        "interactive": {
                                            "button_reply": {"id": "option_1", "title": "Opção 1"}
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    )

    assert messages[0].texto == "1"


def test_parser_converts_list_option_id_to_number():
    messages = parse_whatsapp_payload(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "351900000000",
                                        "id": "m1",
                                        "type": "interactive",
                                        "interactive": {
                                            "list_reply": {"id": "option_4", "title": "Opção 4"}
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    )

    assert messages[0].texto == "4"
