"""S3-compatible driver (AWS S3, MinIO, GCS interop) using boto3 in a worker thread."""

import asyncio
from typing import Any

from app.core.config import settings
from app.core.errors import AppError
from app.core.storage.base import ObjectStorage


class S3Storage(ObjectStorage):
    def __init__(self, client: Any | None = None, bucket: str | None = None) -> None:
        if client is None:
            import boto3  # imported lazily: only the S3 driver needs it

            client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint or None,
                aws_access_key_id=settings.s3_access_key or None,
                aws_secret_access_key=settings.s3_secret_key or None,
                region_name=settings.s3_region or None,
            )
        self.client = client
        self.bucket = bucket or settings.s3_bucket

    async def _write(self, key: str, payload: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self.client.put_object, Bucket=self.bucket, Key=key, Body=payload, ContentType=content_type
        )

    async def _read(self, key: str) -> bytes:
        def read() -> bytes:
            try:
                return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            except self.client.exceptions.NoSuchKey as exc:
                raise AppError("NOT_FOUND") from exc

        return await asyncio.to_thread(read)

    async def _remove(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

    async def content_type(self, key: str) -> str:
        head = await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
        return head.get("ContentType", "application/octet-stream")

    # signed_url() is inherited: it points at the API files endpoint, which decrypts.

    async def presigned_url(self, key: str, ttl_sec: int = 60) -> str:
        """Direct bucket URL. Only for unencrypted objects (S3 would serve ciphertext)."""
        return await asyncio.to_thread(
            self.client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl_sec,
        )
