import bot
import json
import base64
import socketio
import requests
import logging
from telegram.ext import ContextTypes

config = bot.config
client = bot.client
llm = bot.llm
llm_enabled = bot.llm_enabled
changeButton = bot.changeButton
groupId = config["bot"]["groupId"]
websiteId = config["crisp"]["website"]
llm_cfg = config.get("llm", {})
payload = llm_cfg.get("payload", "你是一个简体中文客服，请礼貌、准确、简洁地回复用户。")


def getKey(content: str):
    if len(config["autoreply"]) > 0:
        for x in config["autoreply"]:
            keyword = x.split("|")
            for key in keyword:
                if key in content:
                    return True, config["autoreply"][x]
    return False, None


def getMetas(sessionId):
    metas = client.website.get_conversation_metas(websiteId, sessionId)

    flow = ['📠<b>Crisp消息推送</b>', '']
    if len(metas["email"]) > 0:
        email = metas["email"]
        flow.append(f'📧<b>电子邮箱</b>：{email}')
    if len(metas["data"]) > 0:
        if "Plan" in metas["data"]:
            plan = metas["data"]["Plan"]
            flow.append(f"🪪<b>使用套餐</b>：{plan}")
        if "UsedTraffic" in metas["data"] and "AllTraffic" in metas["data"]:
            used_traffic = metas["data"]["UsedTraffic"]
            all_traffic = metas["data"]["AllTraffic"]
            flow.append(f"🗒<b>流量信息</b>：{used_traffic} / {all_traffic}")
    if len(flow) > 2:
        return '\n'.join(flow)
    return '无额外信息'


async def createSession(data):
    tg_bot = callbackContext.bot
    botData = callbackContext.bot_data
    sessionId = data["session_id"]
    session = botData.get(sessionId)

    metas = getMetas(sessionId)
    if session is None:
        enableAI = llm_enabled
        topic = await tg_bot.create_forum_topic(groupId, data["user"]["nickname"])
        msg = await tg_bot.send_message(
            groupId,
            metas,
            message_thread_id=topic.message_thread_id,
            reply_markup=changeButton(sessionId, enableAI)
        )
        botData[sessionId] = {
            'topicId': topic.message_thread_id,
            'messageId': msg.message_id,
            'enableAI': enableAI
        }
    else:
        try:
            await tg_bot.edit_message_text(metas, groupId, session['messageId'])
        except Exception as error:
            print(error)


def build_llm_messages(data):
    messages = [{"role": "system", "content": payload}]

    nickname = data.get("user", {}).get("nickname") or "客户"
    metas = []
    user_email = data.get("user", {}).get("email")
    if user_email:
        metas.append(f"用户邮箱：{user_email}")
    if nickname:
        metas.append(f"用户昵称：{nickname}")
    if metas:
        messages.append({"role": "system", "content": "\n".join(metas)})

    messages.append({"role": "user", "content": data["content"]})
    return messages


def ask_llm(data):
    model = llm_cfg.get("model", "gpt-4o-mini")
    kwargs = {
        "model": model,
        "messages": build_llm_messages(data)
    }

    temperature = llm_cfg.get("temperature")
    if temperature is not None:
        kwargs["temperature"] = temperature

    max_tokens = llm_cfg.get("maxTokens")
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    response = llm.chat.completions.create(**kwargs)
    content = response.choices[0].message.content
    if content is None:
        return None
    return content.strip()


async def sendMessage(data):
    tg_bot = callbackContext.bot
    botData = callbackContext.bot_data
    sessionId = data["session_id"]
    session = botData.get(sessionId)

    client.website.mark_messages_read_in_conversation(
        websiteId,
        sessionId,
        {"from": "user", "origin": "chat", "fingerprints": [data["fingerprint"]]}
    )

    if data["type"] == "text":
        flow = ['📠<b>消息推送</b>', '']
        flow.append(f"🧾<b>消息内容</b>：{data['content']}")

        autoreply = None
        result, keyword_reply = getKey(data["content"])
        if result is True:
            autoreply = keyword_reply
            flow.append("")
            flow.append(f"💡<b>关键词回复</b>：{autoreply}")
        elif llm is not None and session["enableAI"] is True:
            try:
                autoreply = ask_llm(data)
                if autoreply:
                    flow.append("")
                    flow.append(f"🤖<b>OpenClaw/LLM回复</b>：{autoreply}")
            except Exception as error:
                logging.exception("LLM/OpenClaw reply failed: %s", error)
                flow.append("")
                flow.append("⚠️<b>AI 回复失败</b>：请检查 OpenClaw/兼容接口配置")

        if autoreply is not None:
            query = {
                "type": "text",
                "content": autoreply,
                "from": "operator",
                "origin": "chat",
                "user": {
                    "nickname": '智能客服',
                    "avatar": 'https://img.ixintu.com/download/jpg/20210125/8bff784c4e309db867d43785efde1daf_512_512.jpg'
                }
            }
            client.website.send_message_in_conversation(websiteId, sessionId, query)
        await tg_bot.send_message(
            groupId,
            '\n'.join(flow),
            message_thread_id=session["topicId"]
        )
    elif data["type"] == "file" and str(data["content"]["type"]).count("image") > 0:
        await tg_bot.send_photo(
            groupId,
            data["content"]["url"],
            message_thread_id=session["topicId"]
        )
    else:
        print("Unhandled Message Type : ", data["type"])


sio = socketio.AsyncClient(reconnection_attempts=5, logger=True)


@sio.on("connect")
async def connect():
    await sio.emit("authentication", {
        "tier": "plugin",
        "username": config["crisp"]["id"],
        "password": config["crisp"]["key"],
        "events": [
            "message:send",
            "session:set_data"
        ]
    })


@sio.on("unauthorized")
async def unauthorized(data):
    print('Unauthorized: ', data)


@sio.event
async def connect_error():
    print("The connection failed!")


@sio.event
async def disconnect():
    print("Disconnected from server.")


@sio.on("message:send")
async def messageForward(data):
    if data["website_id"] != websiteId:
        return
    await createSession(data)
    await sendMessage(data)


# Meow!
def getCrispConnectEndpoints():
    url = "https://api.crisp.chat/v1/plugin/connect/endpoints"

    authtier = base64.b64encode(
        (config["crisp"]["id"] + ":" + config["crisp"]["key"]).encode("utf-8")
    ).decode("utf-8")
    headers = {"X-Crisp-Tier": "plugin", "Authorization": "Basic " + authtier}
    response = requests.request("GET", url, headers=headers, data="")
    endPoint = json.loads(response.text).get("data").get("socket").get("app")
    return endPoint


async def exec(context: ContextTypes.DEFAULT_TYPE):
    global callbackContext
    callbackContext = context
    await sio.connect(
        getCrispConnectEndpoints(),
        transports="websocket",
        wait_timeout=10,
    )
    await sio.wait()
