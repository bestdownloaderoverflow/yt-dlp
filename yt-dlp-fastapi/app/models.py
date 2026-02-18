from typing import Optional, Union
from pydantic import BaseModel, HttpUrl


class VideoRequest(BaseModel):
    url: HttpUrl
    format_id: Optional[str] = None
    download_mp3: bool = False


class TikTokRequest(BaseModel):
    url: HttpUrl


class ProcessResponse(BaseModel):
    download_id: str
    title: str
    duration: Optional[Union[int, float]]
    thumbnail: Optional[str]
    formats: list


class ProgressResponse(BaseModel):
    download_id: str
    status: str
    progress: float
    speed: Optional[str]
    eta: Optional[str]
    filename: Optional[str]
    error: Optional[str]
