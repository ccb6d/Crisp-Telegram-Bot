import os
import re
import json
import base64
import socketio
import requests
import logging
import yaml
from telegram.ext import ContextTypes

import bot

config = bot.config
client = bot.client
llm = bot.llm
llm_enabled = bot.llm_enabled
changeButton = bot.changeButton
groupId = config["bot"]["groupId"]
websiteId = config["crisp"]["website"]
llm_cfg = config.get("llm", {})
payload = llm_cfg.get("payload", "你是一个简体中文客服，请礼貌、准确、简洁地回复用户。")
kb_cfg = config.get("knowledge_base", {})
KB_EXTENSIONS = {".txt", ".md", ".json", ".yml", ".yaml"}
knowledge_docs = []


def tokenize(text):
    if not text:
        return []
    lowered = str(text).lower()
    english_words = re.findall(r"[a-z0-9_\-\.]{2,}", lowered)
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", lowered)
    tokens = []
    tokens.extend(english_words)
    for chunk in chinese_chunks:
        if len(chunk) <= 4:
            tokens.append(chunk)
        else:
            for i in range(len(chunk) - 1):
                tokens.append(chunk[i:i+2])
            for i in range(len(chunk) - 2):
                tokens.append(chunk[i:i+3])
    return tokens


def load_text_from_file(path):
    suffix = os.path.splitext(path)[1].lower()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read()
    except UnicodeDecodeError:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            raw = f.read()

    if suffix == '.json':
        try:
            obj = json.loads(raw)
            return json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            return raw
    if suffix in {'.yml', '.yaml'}:
        try:
            obj = yaml.safe_load(raw)
            return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False)
        except Exception:
            return raw
    return raw


def chunk_text(text, chunk_size=1200, overlap=200):
    text = (text or '').strip()
    if not text:
        return []
    chunks = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + chunk_size, length)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return chunks


def load_knowledge_base():
    docs = []
    enabled = kb_cfg.get('enabled', False)
    directory = kb_cfg.get('directory', './knowledge')
    if not enabled:
        logging.info('Knowledge base disabled')
        return docs
    if not os.path.isabs(directory):
        directory = os.path.join(os.getcwd(), directory)
    if not os.path.isdir(directory):
        logging.warning('Knowledge base directory not found: %s', directory)
        return docs

    for root, _, files in os.walk(directory):
        for name in files:
            suffix = os.path.splitext(name)[1].lower()
            if suffix not in KB_EXTENSIONS:
                continue
            path = os.path.join(root, name)
            try:
                content = load_text_from_file(path)
                for idx, chunk in enumerate(chunk_text(content)):
                    docs.append({
                        'path': os.path.relpath(path, directory),
                        'index': idx,
                        'content': chunk,
                        'tokens': set(tokenize(chunk))
                    })
            except Exception as error:
                logging.warning('Failed to load knowledge file %s: %s', path, error)
    logging.info('Knowledge base loaded: %s chunks', len(docs))
    return docs


def search_knowledge(query, limit=None):
    if not knowledge_docs:
        return []
    if limit is None:
        limit = kb_cfg.get('topK', 3)
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return []
    scored = []
    for doc in knowledge_docs:
        overlap = query_tokens & doc['tokens']
        score = len(overlap)
        if score <= 0:
            continue
        scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in scored[:limit]]


def build_knowledge_prompt(query):
    matches = search_knowledge(query)
    if not matches:
        return None, []
    blocks = []
    for doc in matches:
        blocks.append(f"[来源: {doc['path']}#{doc['index']}]\n{doc['content']}")
    prompt = "以下是从本地知识库中检索到的参考内容。请优先依据这些内容回答；如果知识库无法确认事实，请明确说明，不要编造。\n\n" + "\n\n---\n\n".join(blocks)
    return prompt, matches


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
        flow.append(f'📧<b>电子邮箱</b>：{metas["email"]}')
    if len(metas["data"]) > 0:
        if "Plan" in metas["data"]:
            flow.append(f"🪪<b>使用套餐</b>：{metas['data']['Plan']}")
        if "UsedTraffic" in metas["data"] and "AllTraffic" in metas["data"]:
            flow.append(f"🗒<b>流量信息</b>：{metas['data']['UsedTraffic']} / {metas['data']['AllTraffic']}")
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


def build_llm_messages(data, knowledge_prompt=None):
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
    if knowledge_prompt:
        messages.append({"role": "system", "content": knowledge_prompt})
    messages.append({"role": "user", "content": data["content"]})
    return messages


def ask_llm(data, knowledge_prompt=None):
    model = llm_cfg.get("model", "gpt-4o-mini")
    kwargs = {
        "model": model,
        "messages": build_llm_messages(data, knowledge_prompt)
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
                knowledge_prompt, knowledge_hits = build_knowledge_prompt(data["content"])
                autoreply = ask_llm(data, knowledge_prompt)
                if knowledge_hits:
                    refs = '、'.join([f"{x['path']}#{x['index']}" for x in knowledge_hits])
                    flow.append("")
                    flow.append(f"📚<b>知识库命中</b>：{refs}")
                if autoreply:
                    flow.append("")
                    flow.append(f"🤖<b>AI回复</b>：{autoreply}")
            except Exception as error:
                logging.exception("LLM reply failed: %s", error)
                flow.append("")
                flow.append("⚠️<b>AI 回复失败</b>：请检查 LLM 接口或知识库配置")

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
        await tg_bot.send_message(groupId, '\n'.join(flow), message_thread_id=session["topicId"])
    elif data["type"] == "file" and str(data["content"]["type"]).count("image") > 0:
        await tg_bot.send_photo(groupId, data["content"]["url"], message_thread_id=session["topicId"])
    else:
        print("Unhandled Message Type : ", data["type"])


sio = socketio.AsyncClient(reconnection_attempts=5, logger=True)


@sio.on("connect")
async def connect():
    await sio.emit("authentication", {
        "tier": "plugin",
        "username": config["crisp"]["id"],
        "password": config["crisp"]["key"],
        "events": ["message:send", "session:set_data"]
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


def getCrispConnectEndpoints():
    url = "https://api.crisp.chat/v1/plugin/connect/endpoints"
    authtier = base64.b64encode((config["crisp"]["id"] + ":" + config["crisp"]["key"]).encode("utf-8")).decode("utf-8")
    headers = {"X-Crisp-Tier": "plugin", "Authorization": "Basic " + authtier}
    response = requests.request("GET", url, headers=headers, data="")
    return json.loads(response.text).get("data").get("socket").get("app")


async def exec(context: ContextTypes.DEFAULT_TYPE):
    global callbackContext, knowledge_docs
    callbackContext = context
    knowledge_docs = load_knowledge_base()
    await sio.connect(getCrispConnectEndpoints(), transports="websocket", wait_timeout=10)
    await sio.wait()
