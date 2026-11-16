"""M12 rules: catalog by role, per-user layout, gadget data dispatch, notifications (user scoped)."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext
from app.core.errors import AppError, validation_error
from app.core.pagination import PageParams
from app.modules.m12_dashboard.gadgets import GADGETS
from app.modules.m12_dashboard.schemas import (
    CatalogItem,
    CatalogOut,
    GadgetDataOut,
    LayoutIn,
    LayoutItem,
    LayoutOut,
    NotificationOut,
)


async def _catalog_rows(session: AsyncSession, role: str) -> list[dict]:
    rows = await session.execute(
        text(
            "SELECT key, title_key, default_enabled, default_order FROM gadget_catalog "
            "WHERE :role = ANY(available_for) ORDER BY default_order"
        ),
        {"role": role},
    )
    return [dict(r._mapping) for r in rows]


class DashboardService:
    @staticmethod
    async def catalog(ctx: TenantContext, session: AsyncSession) -> CatalogOut:
        return CatalogOut(items=[CatalogItem(**r) for r in await _catalog_rows(session, ctx.role)])

    @staticmethod
    async def layout(ctx: TenantContext, session: AsyncSession) -> LayoutOut:
        available = {r["key"]: r for r in await _catalog_rows(session, ctx.role)}
        saved = (
            await session.execute(
                text(
                    "SELECT gadget_key, size, enabled FROM user_gadgets WHERE user_id = :u ORDER BY position"
                ),
                {"u": ctx.user_id},
            )
        ).all()
        items = [
            LayoutItem(key=r.gadget_key, size=r.size, enabled=r.enabled)
            for r in saved
            if r.gadget_key in available
        ]
        if saved:
            return LayoutOut(items=items, is_default=False)
        return LayoutOut(
            items=[LayoutItem(key=k, enabled=r["default_enabled"]) for k, r in available.items()],
            is_default=True,
        )

    @staticmethod
    async def save_layout(ctx: TenantContext, session: AsyncSession, body: LayoutIn) -> LayoutOut:
        available = {r["key"] for r in await _catalog_rows(session, ctx.role)}
        keys = [g.key for g in body.gadgets]
        errors = [
            {"field": f"gadgets.{i}.key", "code": "gadget_not_available"}
            for i, k in enumerate(keys)
            if k not in available
        ] + [
            {"field": f"gadgets.{i}.key", "code": "duplicate"}
            for i, k in enumerate(keys)
            if keys.index(k) != i
        ]
        if errors:
            raise validation_error(errors)
        await session.execute(text("DELETE FROM user_gadgets WHERE user_id = :u"), {"u": ctx.user_id})
        for position, g in enumerate(body.gadgets):
            await session.execute(
                text(
                    "INSERT INTO user_gadgets (user_id, gadget_key, position, size, enabled) "
                    "VALUES (:u, :k, :p, :s, :e)"
                ),
                {"u": ctx.user_id, "k": g.key, "p": position, "s": g.size, "e": g.enabled},
            )
        return await DashboardService.layout(ctx, session)

    @staticmethod
    async def gadget_data(ctx: TenantContext, session: AsyncSession, key: str) -> GadgetDataOut:
        """A gadget the role may not use is rejected (403); an unknown key is 404."""
        known = (
            await session.execute(text("SELECT available_for FROM gadget_catalog WHERE key = :k"), {"k": key})
        ).scalar_one_or_none()
        if known is None or key not in GADGETS:
            raise AppError("NOT_FOUND")
        if ctx.role not in known:
            raise AppError("FORBIDDEN")
        return GadgetDataOut(key=key, data=await GADGETS[key](ctx, session))

    # ------------------------------------------------------------------ notifications

    @staticmethod
    async def notifications(
        ctx: TenantContext, session: AsyncSession, params: PageParams, *, unread: bool
    ) -> tuple[list[NotificationOut], int]:
        where = "user_id = :u" + (" AND read_at IS NULL" if unread else "")
        total = (
            await session.execute(
                text(f"SELECT count(*) FROM notifications WHERE {where}"), {"u": ctx.user_id}
            )
        ).scalar_one()
        rows = await session.execute(
            text(
                "SELECT id, type, payload, entity_type, entity_id, read_at, created_at FROM notifications "
                f"WHERE {where} ORDER BY created_at DESC, id OFFSET :off LIMIT :lim"
            ),
            {"u": ctx.user_id, "off": params.offset, "lim": params.limit},
        )
        return [NotificationOut(**dict(r._mapping)) for r in rows], total

    @staticmethod
    async def unread_count(ctx: TenantContext, session: AsyncSession) -> int:
        row = await session.execute(
            text("SELECT count(*) FROM notifications WHERE user_id = :u AND read_at IS NULL"),
            {"u": ctx.user_id},
        )
        return row.scalar_one()

    @staticmethod
    async def mark_read(ctx: TenantContext, session: AsyncSession, notification_id: UUID) -> None:
        """Only your own notification; another user's id is 404."""
        row = await session.execute(
            text(
                "UPDATE notifications SET read_at = coalesce(read_at, now()) WHERE id = :i AND user_id = "
                ":u RETURNING id"
            ),
            {"i": notification_id, "u": ctx.user_id},
        )
        if row.first() is None:
            raise AppError("NOT_FOUND")

    @staticmethod
    async def mark_all_read(ctx: TenantContext, session: AsyncSession) -> int:
        rows = await session.execute(
            text(
                "UPDATE notifications SET read_at = now() WHERE user_id = :u AND read_at IS NULL RETURNING id"
            ),
            {"u": ctx.user_id},
        )
        return len(rows.all())
