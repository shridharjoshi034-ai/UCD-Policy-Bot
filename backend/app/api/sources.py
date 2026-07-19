from app.schemas import SourceMetaData
from fastapi import APIRouter
from app.services.source_services import get_source_list

router = APIRouter(prefix = "/sources", tags = ["sources"])

@router.get("", response_model = list[SourceMetaData])
async def source_list():
    return get_source_list() 

