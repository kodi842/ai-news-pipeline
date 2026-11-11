"""Доставка в VK-группу города (спека §8: кросс-постинг).

Тот же интерфейс, что у Telegram-адаптера (`publish` → DeliveryResult), поэтому
очередь про платформу ничего не знает — добавить MAX/OK можно третьим классом.

Публикация на стену — три шага VK API: получить сервер загрузки, залить файл,
сохранить фото и приложить к `wall.post`. Картинка не залилась — пост всё равно
выходит текстом: новость важнее иллюстрации.

Включается ПО ГОРОДАМ (`vk.enabled` в channels.yaml, §41): группа одного
города может быть готова, а другого — ещё нет. Токен у каждого города свой —
сообщество выдаёт ключ только на себя; сервисный ключ фазы 2 на стену писать
не умеет вовсе.
"""
from __future__ import annotations

import logging

from tgtraff.publisher.delivery import DeliveryError, DeliveryResult

log = logging.getLogger("tgtraff.publisher.vk")


async def _httpx_upload(url: str, path: str) -> dict:
    import httpx

    with open(path, "rb") as fh:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, files={"photo": (path.split("/")[-1], fh)})
            r.raise_for_status()
            return r.json()


class VkDelivery:
    """Доставка на стену сообщества.

    Токен — СВОЙ у каждого города (§41): ключ выдаёт конкретная группа, и
    одним ключом на сеть в чужую группу не написать. Поэтому адаптер берёт
    токен из города и держит по клиенту на токен, а не один на всех;
    `api_factory(token)` создаёт клиента, `api` — готовый клиент на все города
    (так проще в тестах и в разовых скриптах)."""

    platform = "vk"

    def __init__(self, api=None, uploader=None, api_factory=None):
        if api is None and api_factory is None:
            raise ValueError("нужен api или api_factory")
        self.api = api
        self._factory = api_factory
        self._upload = uploader or _httpx_upload
        self._clients: dict[str, object] = {}

    def api_for(self, city):
        """Клиент под токен города. Один клиент на токен: новая сессия на
        каждый пост — лишние коннекты и лишний шанс поймать лимит частоты."""
        if self._factory is None:
            return self.api
        token = city.vk.token
        if not token:
            raise DeliveryError(
                f"у города {city.city} включён ВК, но нет токена сообщества "
                "(права wall+photos) — пост на стену не уйдёт")
        if token not in self._clients:
            self._clients[token] = self._factory(token)
        return self._clients[token]

    async def publish(self, city, text: str, media_path: str | None) -> DeliveryResult:
        api = self.api_for(city)
        group_id = int(city.vk.group_id)
        attachment = await self._attach(api, group_id, media_path) if media_path else None
        try:
            response = await api.call(
                "wall.post", owner_id=-group_id, from_group=1, message=text,
                **({"attachments": attachment} if attachment else {}))
        except Exception as exc:                  # noqa: BLE001 — VkError или сеть
            raise DeliveryError(f"ВК не принял пост: {exc}") from exc

        post_id = str(response.get("post_id"))
        return DeliveryResult(self.platform, str(-group_id), post_id,
                              f"https://vk.com/wall-{group_id}_{post_id}")

    async def _attach(self, api, group_id: int, media_path: str) -> str | None:
        try:
            server = await api.call("photos.getWallUploadServer", group_id=group_id)
            uploaded = await self._upload(server["upload_url"], media_path)
            saved = await api.call(
                "photos.saveWallPhoto", group_id=group_id, server=uploaded["server"],
                photo=uploaded["photo"], hash=uploaded["hash"])
        except Exception as exc:                  # noqa: BLE001
            log.warning("ВК: картинка не загрузилась (%s) — пост уйдёт текстом", exc)
            return None
        photo = saved[0]
        return f"photo{photo['owner_id']}_{photo['id']}"
