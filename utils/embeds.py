"""Reusable embed builders - every reply in the bot should use one of these."""
import discord
from datetime import datetime, timezone
from config import constants


def base_embed(title: str = None, description: str = None, color: int = constants.COLOR_DEFAULT) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    return embed


def success_embed(description: str, title: str = f"{constants.EMOJI_SUCCESS} Success") -> discord.Embed:
    return base_embed(title=title, description=description, color=constants.COLOR_SUCCESS)


def error_embed(description: str, title: str = f"{constants.EMOJI_ERROR} Error") -> discord.Embed:
    return base_embed(title=title, description=description, color=constants.COLOR_ERROR)


def warning_embed(description: str, title: str = f"{constants.EMOJI_WARNING} Warning") -> discord.Embed:
    return base_embed(title=title, description=description, color=constants.COLOR_WARNING)


def info_embed(description: str, title: str = None) -> discord.Embed:
    return base_embed(title=title, description=description, color=constants.COLOR_INFO)

