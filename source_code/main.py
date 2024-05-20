from config import NGROK_URL, TOKEN, OPERATOR_CHAT_ID
from models import *
from db import *
from fastapi import FastAPI, Request, HTTPException, Depends
import aiohttp
from redis_client import redis
from chatgpt import ChatGPT
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select


app = FastAPI()
openai = ChatGPT()


webhook_url = f"{NGROK_URL}/webhook/"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

set_webhook_url = f"{BASE_URL}/setWebhook?url="
delete_webhook_url = f"{BASE_URL}/deleteWebhook"


async def manage_webhooks(delete_webhook_url, set_webhook_url, webhook_url):
    async with aiohttp.ClientSession() as session:
        async with session.get(delete_webhook_url) as response:
            if response.status == 200:
                print("Webhook deleted successfully")
            else:
                print(f"Failed to delete webhook: {response.status}")
                return

        async with session.get(set_webhook_url + webhook_url) as response:
            if response.status == 200:
                print("Webhook set successfully")
            else:
                print(f"Failed to set webhook: {response.status}")
                return


@app.on_event("startup")
async def startup_event():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await manage_webhooks(delete_webhook_url, set_webhook_url, webhook_url)


async def get_redis_status(chat_id: int):
    chat_status = (await redis.get(chat_id)) or b""
    return chat_status.decode('utf-8')


async def send_message_to_chat(text: str, chat_id: int):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if chat_id != OPERATOR_CHAT_ID:
        payload["reply_markup"] = {
            "keyboard": [[{"text": "Подключить/отключить оператора"}]],
            "resize_keyboard": True,
            "one_time_keyboard": True
        }
    # Также можно сделать инлайнами, но мне нравится кнопка
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            if response.status != 200:
                print(f"Failed to send message. Status code: {response.status}")
            else:
                print("Message sent successfully")
                print(text)


async def process_openai_message(text: str, chat_id: int):
    await openai.send_message(text)
    response = await openai.get_response()
    await send_message_to_chat(response, chat_id=chat_id)


async def handle_operator_message(message):
    try:
        client_id, reply = message.split(':', 1)
        await send_message_to_chat(f"Ответ оператора: {reply.strip()}", client_id.strip())
    except ValueError:
        await send_message_to_chat("Неверный формат сообщения. Используйте формат 'chat_id: сообщение'.",
                                   OPERATOR_CHAT_ID)


@app.post("/webhook/")
async def webhook(req: Request, db: AsyncSession = Depends(get_db)):
    '''
    На вход принимается телеграммовский пэйлоад, на выходе в зависимости от направления сообщения получаем отправленное сообщение в ТГ
    '''
    try:
        data = await req.json()
        chat_id = data['message']['chat']['id']
        text = data['message']['text'].strip().lower()
        dialogue_query = await db.execute(select(Dialogue).filter_by(chat_id=str(chat_id)))
        dialogue = dialogue_query.scalars().first()

        if not dialogue:
            dialogue = Dialogue(chat_id=str(chat_id))
            db.add(dialogue)
            await db.commit()
            await db.refresh(dialogue)

        message = Message(text=text, dialogue_id=dialogue.id)
        db.add(message)
        await db.commit()

        if chat_id == OPERATOR_CHAT_ID:
            await handle_operator_message(text)
            return

        chat_status = await get_redis_status(chat_id)

        if text == 'подключить/отключить оператора':
            if chat_status == 'bot':
                await send_message_to_chat("Оператор подключен к диалогу", chat_id)
                await redis.set(chat_id, "operator")
                # не могу достучаться и получаю 404, но вот код который должен вытащить историю сообщений
                # чтобы оператор был в контексте предыдущего диалога с пользователем
                # плюс есть механизм, который сообщит оператору, что бот функционирует не корректно
                # url = f"https://api.telegram.org/bot{TOKEN}/getChatHistory?chat_id={chat_id}"
                # async with aiohttp.ClientSession() as session:
                #     async with session.get(url) as response:
                #         if response.status == 200:
                #             message_history = response.json()
                #             await send_message_to_chat(f"Сообщение от пользователя {chat_id}\n {message_history}", OPERATOR_CHAT_ID)
                #         else:
                #             await send_message_to_chat("Что-то пошло не так, мы сообщили о проблеме разработчикам", chat_id)
                #             await send_message_to_chat(f"Ошибка при попытке достать историю:\n{response.text}", OPERATOR_CHAT_ID)
                #
                #
                # mock сообщение
                await send_message_to_chat(f"Сообщение от пользователя {chat_id}\nОтветьте в формате id: "
                                           f"сообщение\nmessage history", OPERATOR_CHAT_ID)
            else:
                await send_message_to_chat("Оператор отключен от диалога", chat_id)
                await redis.set(chat_id, "bot")
            return

        if text == "/start":
            await send_message_to_chat("Привет! Я ваш новый бот. Я имею прямую связь с чатгпт, поэтому можешь задать "
                                       "любой вопрос.\nДля разговора с оператором нажмите кнопку под сообщением\nДля "
                                       "окончания диалога с оператором снова нажмите кнопку"
                                       , chat_id)
            await redis.set(chat_id, "bot")
            return

        if chat_status == "operator":
            await send_message_to_chat(f"Сообщение от пользователя {chat_id}\n{text}", OPERATOR_CHAT_ID)
        elif chat_status == "bot":
            await process_openai_message(text, chat_id)
    except Exception as e:
        print("Exception" + str(e))


# TEST PURPOSE

@app.get("/redis_ping/")
async def redis_ping():
    try:
        pong = await redis.ping()
        if pong:
            return {"message": "Redis is connected"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Redis connection failed")


@app.post("/redis_set_get/")
async def set_and_get_redis_key(key: str, value: str):
    try:
        await redis.set(key, value)
        stored_value = await redis.get(key)
        return {"message": "Key set and retrieved successfully", "key": key, "stored_value": stored_value}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to set and get key in Redis")


@app.get('/redis_get/')
async def get_redis_key(key: str):
    stored_value = await redis.get(key)
    return {"val": stored_value}
