from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.integrations.whatsapp.parser import parse_whatsapp_payload


def test_get_webhook_with_correct_token(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "test-token")
    get_settings.cache_clear()

    response = TestClient(client).get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-token",
            "hub.challenge": "abc123",
        },
    )

    assert response.status_code == 200
    assert response.text == "abc123"


def test_get_webhook_with_incorrect_token(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "test-token")
    get_settings.cache_clear()

    response = TestClient(client).get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "abc123",
        },
    )

    assert response.status_code == 403


def test_webhook_com_status_delivered_nao_envia_resposta(client, monkeypatch):
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.fake",
                                    "status": "delivered",
                                    "timestamp": "1730000000",
                                    "recipient_id": "556198266551",
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    response = TestClient(client).post("/webhook/whatsapp", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "messages": []}


def test_payload_fake_meta_e_parseado_corretamente():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1502228507690349",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "556196870361",
                                "phone_number_id": "1148807428322172",
                            },
                            "contacts": [{"profile": {"name": "Danilo Fukuda"}, "wa_id": "556198266551"}],
                            "messages": [
                                {
                                    "from": "556198266551",
                                    "id": "wamid.fake",
                                    "timestamp": "1780000000",
                                    "type": "location",
                                    "location": {
                                        "latitude": 38.7223,
                                        "longitude": -9.1393,
                                        "name": "Obra teste Lisboa",
                                        "address": "Lisboa, Portugal",
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }

    messages = parse_whatsapp_payload(payload)

    assert len(messages) == 1
    assert messages[0].telefone == "556198266551"
    assert messages[0].tipo == "location"
    assert messages[0].latitude == 38.7223
    assert messages[0].longitude == -9.1393
    assert messages[0].location_name == "Obra teste Lisboa"
    assert messages[0].location_address == "Lisboa, Portugal"
    assert messages[0].texto == (
        "Obra teste Lisboa - Lisboa, Portugal - https://www.google.com/maps?q=38.7223,-9.1393"
    )


def test_payload_interactive_button_reply_vira_texto_da_opcao():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "556198266551",
                                    "id": "wamid.button",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {"id": "1", "title": "Sim"},
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    messages = parse_whatsapp_payload(payload)

    assert len(messages) == 1
    assert messages[0].telefone == "556198266551"
    assert messages[0].tipo == "interactive"
    assert messages[0].texto == "1"


def test_payload_interactive_button_reply_preserva_id_especifico():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "556198266551",
                                    "id": "wamid.button",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {
                                            "id": "pedido_mao_obra_sim",
                                            "title": "✅ Sim",
                                        },
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    messages = parse_whatsapp_payload(payload)

    assert messages[0].texto == "pedido_mao_obra_sim"


def test_payload_location_sem_coordenadas_validas_nao_gera_texto():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "556198266551",
                                    "id": "wamid.location",
                                    "type": "location",
                                    "location": {"name": "Obra sem coordenadas"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    messages = parse_whatsapp_payload(payload)

    assert messages[0].tipo == "location"
    assert messages[0].texto is None


def test_payload_contacts_extrai_nome_e_prefere_wa_id():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "351900000000",
                                    "id": "wamid.contact",
                                    "type": "contacts",
                                    "contacts": [
                                        {
                                            "name": {"formatted_name": "Cliente WhatsApp"},
                                            "phones": [
                                                {
                                                    "phone": "+351 913 000 111",
                                                    "wa_id": "351913000999",
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    messages = parse_whatsapp_payload(payload)

    assert len(messages) == 1
    assert messages[0].tipo == "contacts"
    assert messages[0].contact_name == "Cliente WhatsApp"
    assert messages[0].contact_phone == "351913000999"


def test_payload_contacts_usa_primeiro_phone_quando_nao_ha_wa_id():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "351900000000",
                                    "id": "wamid.contact",
                                    "type": "contacts",
                                    "contacts": [
                                        {
                                            "formatted_name": "Empresa Cliente",
                                            "phones": [{"phone": "+351 914 000 222"}],
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    messages = parse_whatsapp_payload(payload)

    assert messages[0].contact_name == "Empresa Cliente"
    assert messages[0].contact_phone == "+351 914 000 222"
