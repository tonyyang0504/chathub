"""
Marketplace API Routes
Endpoints for browsing, installing, and uninstalling community tools.
"""

import re
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Optional

from app.database import get_db, CustomToolListing, CustomToolInstall, BuiltTool, User
from app.auth.utils import get_current_user_optional
CUSTOM_TOOLS_DIR = Path(__file__).parent / "custom"

_grad_re = re.compile(r'linear-gradient\(135deg,\s*(#[0-9a-fA-F]{6})\s+0%,\s*(#[0-9a-fA-F]{6})\s+100%\)')


def _resolve_gradient(name: str, db_start: str, db_end: str) -> tuple:
    """Parse actual gradient from template HTML, falling back to DB values."""
    g_start = db_start or '#6366f1'
    g_end = db_end or '#8b5cf6'
    tpl_path = CUSTOM_TOOLS_DIR / name / "templates" / f"{name}.html"
    if tpl_path.exists():
        try:
            header = tpl_path.read_text()[:500]
            m = _grad_re.search(header)
            if m:
                g_start = m.group(1)
                g_end = m.group(2)
        except Exception:
            pass
    return g_start, g_end


router = APIRouter(prefix="/tools/api/marketplace", tags=["Marketplace"])


@router.get("/listings")
async def browse_listings(
    request: Request,
    category: Optional[str] = None,
    search: Optional[str] = None,
    listing_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Browse published marketplace listings."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    query = db.query(CustomToolListing).filter(
        CustomToolListing.status == "published"
    )

    if listing_type and listing_type != "all":
        query = query.filter(CustomToolListing.listing_type == listing_type)

    if category and category != "all":
        query = query.filter(CustomToolListing.category == category)

    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                CustomToolListing.display_name.ilike(search_term),
                CustomToolListing.description.ilike(search_term),
                CustomToolListing.name.ilike(search_term)
            )
        )

    total = query.count()
    listings = query.order_by(CustomToolListing.install_count.desc()).offset(offset).limit(limit).all()

    installed_ids = set()
    installs = db.query(CustomToolInstall.listing_id).filter(
        CustomToolInstall.user_id == user.id
    ).all()
    installed_ids = {i[0] for i in installs}

    result_listings = []
    for l in listings:
        g_start, g_end = _resolve_gradient(l.name, l.gradient_start, l.gradient_end)
        result_listings.append({
            "id": l.id,
            "name": l.name,
            "display_name": l.display_name,
            "description": l.description,
            "long_description": l.long_description,
            "category": l.category,
            "version": l.version,
            "icon": l.icon,
            "gradient_start": g_start,
            "gradient_end": g_end,
            "install_count": l.install_count,
            "listing_type": l.listing_type or "tool",
            "author_name": l.author.name or l.author.email if l.author else "Unknown",
            "is_installed": l.id in installed_ids,
            "is_own": l.author_id == user.id,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        })

    return {
        "total": total,
        "listings": result_listings,
    }


@router.get("/listings/{listing_id}")
async def get_listing_detail(
    request: Request,
    listing_id: int,
    db: Session = Depends(get_db)
):
    """Get detailed info about a marketplace listing."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    listing = db.query(CustomToolListing).filter(
        CustomToolListing.id == listing_id,
        CustomToolListing.status == "published"
    ).first()

    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    install = db.query(CustomToolInstall).filter(
        CustomToolInstall.user_id == user.id,
        CustomToolInstall.listing_id == listing_id
    ).first()

    g_start, g_end = _resolve_gradient(listing.name, listing.gradient_start, listing.gradient_end)

    return {
        "id": listing.id,
        "name": listing.name,
        "display_name": listing.display_name,
        "description": listing.description,
        "long_description": listing.long_description,
        "category": listing.category,
        "version": listing.version,
        "icon": listing.icon,
        "gradient_start": g_start,
        "gradient_end": g_end,
        "install_count": listing.install_count,
        "listing_type": listing.listing_type or "tool",
        "tool_md_content": listing.tool_md_content,
        "author_name": listing.author.name or listing.author.email if listing.author else "Unknown",
        "is_installed": install is not None,
        "is_own": listing.author_id == user.id,
        "created_at": listing.created_at.isoformat() if listing.created_at else None,
    }


@router.post("/install/{listing_id}")
async def install_tool(
    request: Request,
    listing_id: int,
    db: Session = Depends(get_db)
):
    """Install a marketplace tool. Creates a BuiltTool for the user."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    listing = db.query(CustomToolListing).filter(
        CustomToolListing.id == listing_id,
        CustomToolListing.status == "published"
    ).first()

    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    existing = db.query(CustomToolInstall).filter(
        CustomToolInstall.user_id == user.id,
        CustomToolInstall.listing_id == listing_id
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="Tool already installed")

    tool = BuiltTool(
        user_id=user.id,
        name=f"{listing.name}-installed",
        display_name=listing.display_name,
        description=listing.description,
        icon=listing.icon,
        gradient_start=listing.gradient_start,
        gradient_end=listing.gradient_end,
        tool_md_content=listing.tool_md_content,
        is_active=True,
    )
    db.add(tool)

    install = CustomToolInstall(
        user_id=user.id,
        listing_id=listing_id,
        installed_version=listing.version
    )
    db.add(install)

    listing.install_count = (listing.install_count or 0) + 1

    db.commit()

    return {"message": "Tool installed successfully", "tool_id": tool.id}


@router.delete("/uninstall/{listing_id}")
async def uninstall_tool(
    request: Request,
    listing_id: int,
    db: Session = Depends(get_db)
):
    """Uninstall a marketplace tool."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    install = db.query(CustomToolInstall).filter(
        CustomToolInstall.user_id == user.id,
        CustomToolInstall.listing_id == listing_id
    ).first()

    if not install:
        raise HTTPException(status_code=404, detail="Tool not installed")

    listing = db.query(CustomToolListing).filter(
        CustomToolListing.id == listing_id
    ).first()

    if listing:
        installed_tool = db.query(BuiltTool).filter(
            BuiltTool.user_id == user.id,
            BuiltTool.name == f"{listing.name}-installed",
        ).first()

        if installed_tool:
            db.delete(installed_tool)

        if listing.install_count and listing.install_count > 0:
            listing.install_count -= 1

    db.delete(install)
    db.commit()

    return {"message": "Tool uninstalled successfully"}


@router.get("/installed")
async def list_installed_tools(
    request: Request,
    db: Session = Depends(get_db)
):
    """List user's installed marketplace tools."""
    user = await get_current_user_optional(request, None, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    installs = db.query(CustomToolInstall).filter(
        CustomToolInstall.user_id == user.id
    ).all()

    result = []
    for inst in installs:
        listing = inst.listing
        if listing:
            g_start, g_end = _resolve_gradient(listing.name, listing.gradient_start, listing.gradient_end)
            result.append({
                "install_id": inst.id,
                "listing_id": listing.id,
                "name": listing.name,
                "display_name": listing.display_name,
                "description": listing.description,
                "icon": listing.icon,
                "gradient_start": g_start,
                "gradient_end": g_end,
                "category": listing.category,
                "version": listing.version,
                "installed_version": inst.installed_version,
                "author_name": listing.author.name or listing.author.email if listing.author else "Unknown",
                "installed_at": inst.installed_at.isoformat() if inst.installed_at else None,
            })

    return {"installed": result}
