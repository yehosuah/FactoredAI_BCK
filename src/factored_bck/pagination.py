"""Bounded movement continuation tied to an immutable release and authenticated scope."""

import base64
from datetime import date, datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MovementCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    release_id: str = Field(min_length=1, max_length=200)
    customer_id: str = Field(min_length=1, max_length=100)
    product_id: str = Field(min_length=1, max_length=100)
    before_date: date | None = None
    process_date: date
    transaction_date: datetime
    transaction_id: str = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def source_timestamp(self):
        # The ETL contract uses TIMESTAMP (without timezone).
        if self.transaction_date.tzinfo is not None:
            raise ValueError("invalid_source_timestamp")
        return self

    def encode(self):
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value):
        try:
            if not isinstance(value, str) or not 1 <= len(value) <= 2048:
                raise ValueError("invalid_cursor")
            raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            return cls.model_validate_json(raw)
        except ValueError:
            raise HTTPException(422) from None
