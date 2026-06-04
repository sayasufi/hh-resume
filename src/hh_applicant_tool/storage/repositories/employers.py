from __future__ import annotations

from ..models.employer import EmployerModel
from .base import BaseRepository


class EmployersRepository(BaseRepository):
    __table__ = "employers"
    model = EmployerModel
