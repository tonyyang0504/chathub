from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from pathlib import Path
from app.auth.utils import get_current_user_optional
from app.database import get_db

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "app/templates"))


@router.get("")
async def tool_page(request: Request, db=Depends(get_db)):
    user = await get_current_user_optional(request, None, db)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/auth/login")
    return templates.TemplateResponse("dashboard/index.html", {"request": request, "user": user})
