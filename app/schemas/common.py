from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    """Base for response schemas read directly off SQLAlchemy rows."""

    model_config = ConfigDict(from_attributes=True)


class Message(BaseModel):
    detail: str
