"""
Marketplace API Routes
Endpoints for browsing, installing, and uninstalling community tools.
"""

from datetime import datetime
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Optional

from app.database import get_db, CustomToolListing, CustomToolInstall, ChatHubAgentSkill, User
from app.auth.utils import get_current_user_optional


router = APIRouter(prefix="/tools/api/marketplace", tags=["Marketplace"])


@router.get("/listings")
async def browse_listings(
    request: Request,
    category: Optional[str] = None,
    search: Optional[str] = None,
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

    return {
        "total": total,
        "listings": [
            {
                "id": l.id,
                "name": l.name,
                "display_name": l.display_name,
                "description": l.description,
                "long_description": l.long_description,
                "category": l.category,
                "version": l.version,
                "icon": l.icon,
                "gradient_start": l.gradient_start,
                "gradient_end": l.gradient_end,
                "install_count": l.install_count,
                "author_name": l.author.name or l.author.email if l.author else "Unknown",
                "is_installed": l.id in installed_ids,
                "is_own": l.author_id == user.id,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in listings
        ]
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

    return {
        "id": listing.id,
        "name": listing.name,
        "display_name": listing.display_name,
        "description": listing.description,
        "long_description": listing.long_description,
        "category": listing.category,
        "version": listing.version,
        "icon": listing.icon,
        "gradient_start": listing.gradient_start,
        "gradient_end": listing.gradient_end,
        "install_count": listing.install_count,
        "skill_md_content": listing.skill_md_content,
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
    """Install a marketplace tool. Creates a ChatHubAgentSkill for the user."""
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

    skill = ChatHubAgentSkill(
        user_id=user.id,
        name=f"{listing.name}-installed",
        display_name=listing.display_name,
        description=listing.description,
        icon=listing.icon,
        gradient_start=listing.gradient_start,
        gradient_end=listing.gradient_end,
        skill_md_content=listing.skill_md_content,
        is_active=True,
        tier="community"
    )
    db.add(skill)

    install = CustomToolInstall(
        user_id=user.id,
        listing_id=listing_id,
        installed_version=listing.version
    )
    db.add(install)

    listing.install_count = (listing.install_count or 0) + 1

    db.commit()

    return {"message": "Tool installed successfully", "skill_id": skill.id}


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
        installed_skill = db.query(ChatHubAgentSkill).filter(
            ChatHubAgentSkill.user_id == user.id,
            ChatHubAgentSkill.name == f"{listing.name}-installed",
            ChatHubAgentSkill.tier == "community"
        ).first()

        if installed_skill:
            db.delete(installed_skill)

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
            result.append({
                "install_id": inst.id,
                "listing_id": listing.id,
                "name": listing.name,
                "display_name": listing.display_name,
                "description": listing.description,
                "icon": listing.icon,
                "gradient_start": listing.gradient_start,
                "gradient_end": listing.gradient_end,
                "category": listing.category,
                "version": listing.version,
                "installed_version": inst.installed_version,
                "author_name": listing.author.name or listing.author.email if listing.author else "Unknown",
                "installed_at": inst.installed_at.isoformat() if inst.installed_at else None,
            })

    return {"installed": result}
