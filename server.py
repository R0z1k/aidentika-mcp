"""
MCP-сервер для Aidentika Public API (https://docs.aidentika.com/api).

Даёт Claude инструменты: генерация фото/карточек/видео, редактирование,
статус и получение результата, баланс, цены, категории.

Настройки через переменные окружения (НИКОГДА не храните ключ в коде):
  AIDENTIKA_API_KEY   - ключ формата ak_...  (обязательно)
  MCP_SECRET          - длинная случайная строка; становится частью адреса
                        сервера, защищает от чужих вызовов (обязательно)
  PORT                - порт (по умолчанию 8000)
  AIDENTIKA_BASE      - базовый адрес API (по умолчанию боевой)
"""
import asyncio
import base64
import io
import os
import uuid
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from PIL import Image as PILImage

API_KEY = os.environ.get("AIDENTIKA_API_KEY", "")
BASE = os.environ.get("AIDENTIKA_BASE", "https://api.aidentika.com/api/v1/public").rstrip("/")
SECRET = os.environ.get("MCP_SECRET", "")
PORT = int(os.environ.get("PORT", "8000"))

if not API_KEY:
    raise SystemExit("Не задан AIDENTIKA_API_KEY")
if len(SECRET) < 16:
    raise SystemExit("MCP_SECRET должен быть случайной строкой от 16 символов")

mcp = FastMCP(
    "aidentika",
    instructions=(
        "Инструменты для генерации и редактирования картинок товаров, карточек "
        "маркетплейсов и коротких видео через Aidentika. Генерация асинхронная: "
        "сначала запускаете generate_*, затем wait_for_result с action_id. "
        "Перед платными действиями проверяйте get_balance и get_pricing. "
        "Изображения передавайте публичными HTTPS-ссылками (или upload_id)."
    ),
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    json_response=True,
    streamable_http_path=f"/{SECRET}/mcp",
    # адрес хостинга заранее неизвестен, защита от DNS-rebinding здесь не нужна:
    # доступ ограничен секретным путём
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_client: Optional[httpx.AsyncClient] = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=BASE,
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        )
    return _client


class ApiError(Exception):
    pass


async def call(method: str, path: str, *, json: dict | None = None,
               params: dict | None = None, idempotent: bool = False):
    headers = {"Idempotency-Key": str(uuid.uuid4())} if idempotent else None
    r = await client().request(method, path, json=json, params=params, headers=headers)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text[:500]
        raise ApiError(f"Aidentika вернул {r.status_code}: {detail}")
    return r.json()


def img(value: str) -> dict:
    """Строка -> ImageInput API: https-ссылка, upload_id (upl_...) или base64."""
    v = value.strip()
    if v.startswith(("http://", "https://")):
        return {"url": v}
    if v.startswith("upl_"):
        return {"data": v}
    return {"data": v, "media_type": "image/jpeg"}


def clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# ------------------------------------------------------------------ инфо
@mcp.tool()
async def get_balance() -> dict:
    """Баланс искр на аккаунте Aidentika."""
    return await call("GET", "/balance")


@mcp.tool()
async def get_pricing() -> dict:
    """Стоимость операций (генерация, видео, правка) в искрах."""
    return await call("GET", "/pricing")


@mcp.tool()
async def list_categories() -> dict:
    """Категории товаров и доступные концепты съёмки (category_id, concept_id)."""
    return await call("GET", "/categories")


@mcp.tool()
async def analyze_product(image: str) -> dict:
    """Бесплатно определяет категорию, название и ключевые качества товара по фото.
    image: публичная https-ссылка, upload_id или base64."""
    return await call("POST", "/analyze", json={"image": img(image)})


@mcp.tool()
async def upload_image(base64_data: str, media_type: str = "image/jpeg") -> dict:
    """Загружает изображение (base64) и возвращает upload_id, который действует
    1 час и подходит для нескольких генераций. Для больших файлов лучше
    использовать публичные ссылки."""
    return await call("POST", "/upload",
                      json={"image": {"data": base64_data, "media_type": media_type}})


# ------------------------------------------------------------ генерация
@mcp.tool()
async def generate_photo(
    images: list[str],
    category_id: Optional[str] = None,
    concept_id: Optional[str] = None,
    product_name: Optional[str] = None,
    comment: Optional[str] = None,
    photo_style: str = "classic",
    aspect_ratio: str = "3:4",
    resolution: str = "2K",
    project_id: Optional[int] = None,
) -> dict:
    """Запускает генерацию продуктового фото (СПИСЫВАЕТ искры).
    images: 1-5 референсных фото товара (https-ссылки или upload_id).
    photo_style: classic | home. aspect_ratio: 9:16, 4:3, 1:1, 16:9, 3:4.
    resolution: 1K | 2K | 4K (4K дороже на 2 искры).
    Возвращает action_id; дальше вызовите wait_for_result."""
    body = clean(dict(
        images=[img(i) for i in images], category_id=category_id,
        concept_id=concept_id, product_name=product_name, comment=comment,
        photo_style=photo_style, aspect_ratio=aspect_ratio,
        resolution=resolution, project_id=project_id, locale="ru"))
    return await call("POST", "/generate/photo", json=body, idempotent=True)


@mcp.tool()
async def generate_card(
    images: list[str],
    category_id: Optional[str] = None,
    product_name: Optional[str] = None,
    user_text: Optional[str] = None,
    style: str = "infographic",
    creativity: float = 0.5,
    design_key: Optional[str] = None,
    design_reference_image: Optional[str] = None,
    aspect_ratio: str = "3:4",
    project_id: Optional[int] = None,
) -> dict:
    """Запускает генерацию карточки/инфографики для маркетплейса (СПИСЫВАЕТ искры).
    style: infographic (текст, иконки, плашки) | cinematic (чистый кадр; несовместим
    с design_key и design_reference_image). user_text - строки с преимуществами,
    разделитель - перевод строки. creativity 0.0-1.0."""
    body = clean(dict(
        images=[img(i) for i in images], category_id=category_id,
        concept_id="infographic", product_name=product_name, user_text=user_text,
        style=style, aspect_ratio=aspect_ratio, project_id=project_id, locale="ru"))
    if style == "infographic":
        body.update(clean(dict(
            creativity=creativity, design_key=design_key,
            design_reference_image=img(design_reference_image) if design_reference_image else None)))
    return await call("POST", "/generate/card", json=body, idempotent=True)


@mcp.tool()
async def generate_video(
    image: str,
    scenario: str = "",
    duration_sec: int = 5,
    loop_mode: bool = False,
    card_mode: bool = False,
    aspect_ratio: str = "3:4",
    project_id: Optional[int] = None,
) -> dict:
    """Запускает короткое видео по одному изображению (СПИСЫВАЕТ искры, 1-5 минут).
    duration_sec: 5 или 10. Минимальный размер картинки 300x300."""
    body = clean(dict(image=img(image), scenario=scenario, duration_sec=duration_sec,
                      loop_mode=loop_mode, card_mode=card_mode,
                      aspect_ratio=aspect_ratio, project_id=project_id))
    return await call("POST", "/generate/video", json=body, idempotent=True)


@mcp.tool()
async def edit_result(action_id: int, instruction: str) -> dict:
    """Правка готового результата по текстовой инструкции (до 1000 символов,
    СПИСЫВАЕТ искры). Работает только с завершёнными генерациями. Возвращает новый
    action_id; дальше wait_for_result."""
    return await call("POST", f"/edit/{action_id}",
                      json={"instruction": instruction}, idempotent=True)


@mcp.tool()
async def cancel(action_id: int) -> dict:
    """Отменяет генерацию, которая ещё идёт."""
    return await call("POST", f"/cancel/{action_id}")


# --------------------------------------------------------- статус/результат
@mcp.tool()
async def get_status(action_id: int) -> dict:
    """Текущий статус генерации (pending / processing / completed / failed)."""
    return await call("GET", f"/status/{action_id}")


async def fetch_preview(action_id: int, max_side: int = 1600) -> tuple[bytes, str]:
    """Скачивает результат и сжимает до JPEG, чтобы не раздувать контекст."""
    r = await client().get(f"/results/{action_id}/download")
    if r.status_code >= 400:
        raise ApiError(f"Не удалось скачать результат: {r.status_code} {r.text[:200]}")
    ctype = r.headers.get("content-type", "")
    if ctype.startswith("video"):
        raise ApiError("Это видео: откройте его по ссылке result_url в кабинете Aidentika.")
    im = PILImage.open(io.BytesIO(r.content)).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue(), f"{im.width}x{im.height}"


@mcp.tool()
async def wait_for_result(action_id: int, timeout_sec: int = 55):
    """Ждёт завершения генерации (до timeout_sec, максимум 90) и возвращает
    изображение. Если ещё не готово - вернёт статус, вызовите ещё раз.
    Фото обычно готово за 20-60 секунд, видео за 1-5 минут."""
    deadline = asyncio.get_event_loop().time() + min(timeout_sec, 90)
    await asyncio.sleep(min(15, max(0, timeout_sec - 5)))
    status: dict = {}
    while True:
        status = await call("GET", f"/status/{action_id}")
        st = str(status.get("status", "")).lower()
        if st == "completed":
            break
        if st in ("failed", "cancelled", "canceled"):
            return f"Генерация не удалась: {status.get('error_message') or status}"
        if asyncio.get_event_loop().time() >= deadline:
            return f"Ещё не готово (статус: {st}). Вызовите wait_for_result снова с action_id={action_id}."
        await asyncio.sleep(5)
    try:
        data, size = await fetch_preview(action_id)
    except ApiError as e:
        return f"Готово, но показать не удалось: {e}\nresult_url: {status.get('result_url')}"
    return [f"Готово (action_id={action_id}, превью {size}). result_url: {status.get('result_url')}",
            Image(data=data, format="jpeg")]


@mcp.tool()
async def get_result_image(action_id: int):
    """Показывает уже готовый результат (завершённого action_id)."""
    data, size = await fetch_preview(action_id)
    return [f"action_id={action_id}, превью {size}", Image(data=data, format="jpeg")]


@mcp.tool()
async def list_results(limit: int = 10) -> dict:
    """Последние генерации аккаунта."""
    return await call("GET", "/results", params={"limit": limit})


@mcp.tool()
async def list_projects() -> dict:
    """Список проектов в кабинете."""
    return await call("GET", "/projects")


@mcp.tool()
async def create_project(name: str) -> dict:
    """Создаёт проект-контейнер, чтобы группировать связанные генерации."""
    return await call("POST", "/projects", json={"name": name})


if __name__ == "__main__":
    print(f"Aidentika MCP server starting on port {PORT}")
    mcp.run(transport="streamable-http")
