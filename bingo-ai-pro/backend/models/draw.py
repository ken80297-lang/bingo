from pydantic import BaseModel, Field


class DrawUpload(BaseModel):
    issue: str = Field(..., description="期號")
    time: str = Field(..., description="開獎時間")
    numbers: list[int] = Field(..., min_length=20, max_length=20)
    super_number: int | None = None