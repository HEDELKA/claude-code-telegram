"""Command handlers for bot operations."""

import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
from ...projects import PrivateTopicsUnavailableError, load_project_registry
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
from ...storage.models import SessionModel
from ..utils.html_format import escape_html

logger = structlog.get_logger()


def _is_within_root(path: Path, root: Path) -> bool:
    """Check whether path is within root directory."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _get_thread_project_root(
    settings: Settings, context: ContextTypes.DEFAULT_TYPE
) -> Optional[Path]:
    """Get thread project root when strict thread mode is active."""
    if not settings.enable_project_threads:
        return None
    thread_context = context.user_data.get("_thread_context")
    if not thread_context:
        return None
    return Path(thread_context["project_root"]).resolve()


def _is_private_chat(update: Update) -> bool:
    """Return True when update is from a private chat."""
    chat = update.effective_chat
    return bool(chat and getattr(chat, "type", "") == "private")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user = update.effective_user
    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    manager = context.bot_data.get("project_threads_manager")
    sync_section = ""

    if settings.enable_project_threads and settings.project_threads_mode == "private":
        if not _is_private_chat(update):
            await update.message.reply_text(
                "🚫 <b>Режим приватных топиков</b>\n\n"
                "Используйте этого бота в личном чате и запустите там <code>/start</code>.",
                parse_mode="HTML",
            )
            return

    if (
        settings.enable_project_threads
        and settings.project_threads_mode == "private"
        and _is_private_chat(update)
    ):
        if manager is None:
            await update.message.reply_text(
                "❌ <b>Неверная конфигурация топиков проектов</b>\n\n"
                "Менеджер топиков не инициализирован.",
                parse_mode="HTML",
            )
            return

        try:
            sync_result = await manager.sync_topics(
                context.bot,
                chat_id=update.effective_chat.id,
            )
            sync_section = (
                "\n\n🧵 <b>Топики проектов синхронизированы</b>\n"
                f"• Создано: <b>{sync_result.created}</b>\n"
                f"• Повторно использовано: <b>{sync_result.reused}</b>\n"
                f"• Переименовано: <b>{sync_result.renamed}</b>\n"
                f"• Ошибок: <b>{sync_result.failed}</b>\n\n"
                "Используйте топик проекта для работы с кодом."
            )
        except PrivateTopicsUnavailableError:
            await update.message.reply_text(
                manager.private_topics_unavailable_message(),
                parse_mode="HTML",
            )
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user.id,
                    command="start",
                    args=[],
                    success=False,
                )
            return
        except Exception as e:
            sync_section = (
                "\n\n⚠️ <b>Предупреждение синхронизации топиков</b>\n"
                f"{escape_html(str(e))}\n\n"
                "Выполните <code>/sync_threads</code> для повтора."
            )

    welcome_message = (
        f"👋 Добро пожаловать в Claude Code Telegram Bot, {escape_html(user.first_name)}!\n\n"
        f"🤖 Я помогаю вам работать с Claude Code удалённо через Telegram.\n\n"
        f"<b>Доступные команды:</b>\n"
        f"• <code>/help</code> - Подробная справка\n"
        f"• <code>/new</code> - Начать новую сессию Claude\n"
        f"• <code>/ls</code> - Список файлов в текущей директории\n"
        f"• <code>/cd &lt;dir&gt;</code> - Сменить директорию\n"
        f"• <code>/projects</code> - Показать доступные проекты\n"
        f"• <code>/status</code> - Статус сессии\n"
        f"• <code>/actions</code> - Быстрые действия\n"
        f"• <code>/git</code> - Команды Git-репозитория\n\n"
        f"<b>Быстрый старт:</b>\n"
        f"1. Используйте <code>/projects</code> для просмотра проектов\n"
        f"2. Используйте <code>/cd &lt;project&gt;</code> для перехода в проект\n"
        f"3. Отправьте любое сообщение, чтобы начать работу с Claude!\n\n"
        f"🔒 Доступ защищён, все действия журналируются.\n"
        f"📊 Используйте <code>/status</code> для проверки лимитов."
        f"{sync_section}"
    )

    # Add quick action buttons
    keyboard = [
        [
            InlineKeyboardButton(
                "📁 Проекты", callback_data="action:show_projects"
            ),
            InlineKeyboardButton("❓ Справка", callback_data="action:help"),
        ],
        [
            InlineKeyboardButton("🆕 Новая сессия", callback_data="action:new_session"),
            InlineKeyboardButton("📊 Статус", callback_data="action:status"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        welcome_message, parse_mode="HTML", reply_markup=reply_markup
    )

    # Log command
    if audit_logger:
        await audit_logger.log_command(
            user_id=user.id, command="start", args=[], success=True
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    help_text = (
        "🤖 <b>Справка по Claude Code Telegram Bot</b>\n\n"
        "<b>Навигация:</b>\n"
        "• <code>/ls</code> - Список файлов и директорий\n"
        "• <code>/cd &lt;directory&gt;</code> - Перейти в директорию\n"
        "• <code>/pwd</code> - Показать текущую директорию\n"
        "• <code>/projects</code> - Показать доступные проекты\n\n"
        "<b>Команды сессии:</b>\n"
        "• <code>/new</code> - Сбросить контекст и начать новую сессию\n"
        "• <code>/continue [сообщение]</code> - Продолжить последнюю сессию\n"
        "• <code>/end</code> - Завершить сессию и очистить контекст\n"
        "• <code>/status</code> - Статус сессии и лимиты использования\n"
        "• <code>/export</code> - Экспортировать историю сессии\n"
        "• <code>/actions</code> - Быстрые действия по контексту\n"
        "• <code>/git</code> - Информация о Git-репозитории\n\n"
        "<b>Поведение сессий:</b>\n"
        "• Сессии автоматически сохраняются для каждой директории проекта\n"
        "• Смена директории через <code>/cd</code> возобновляет сессию для этого проекта\n"
        "• Используйте <code>/new</code> или <code>/end</code> для явного сброса контекста\n"
        "• Сессии сохраняются при перезапуске бота\n\n"
        "<b>Примеры использования:</b>\n"
        "• <code>cd myproject</code> - Перейти в директорию проекта\n"
        "• <code>ls</code> - Посмотреть содержимое текущей директории\n"
        "• <code>Создай простой Python-скрипт</code> - Попросить Claude написать код\n"
        "• Отправьте файл, чтобы Claude проверил его\n\n"
        "<b>Работа с файлами:</b>\n"
        "• Отправляйте текстовые файлы (.py, .js, .md и др.) для проверки\n"
        "• Claude умеет читать, изменять и создавать файлы\n"
        "• Все операции ограничены вашей разрешённой директорией\n\n"
        "<b>Безопасность:</b>\n"
        "• 🔒 Защита от обхода пути\n"
        "• ⏱️ Ограничение частоты запросов\n"
        "• 📊 Отслеживание и лимиты использования\n"
        "• 🛡️ Валидация и санитизация ввода\n\n"
        "<b>Советы:</b>\n"
        "• Формулируйте запросы конкретно и чётко\n"
        "• Проверяйте <code>/status</code> для мониторинга лимитов\n"
        "• Используйте кнопки быстрых действий\n"
        "• Загруженные файлы автоматически обрабатываются Claude\n\n"
        "Нужна помощь? Обратитесь к администратору."
    )

    await update.message.reply_text(help_text, parse_mode="HTML")


async def sync_threads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Synchronize project topics in the configured forum chat."""
    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    user_id = update.effective_user.id

    if not settings.enable_project_threads:
        await update.message.reply_text(
            "ℹ️ <b>Режим топиков проектов отключён.</b>", parse_mode="HTML"
        )
        return

    manager = context.bot_data.get("project_threads_manager")
    if not manager:
        await update.message.reply_text(
            "❌ <b>Менеджер топиков проектов не инициализирован.</b>", parse_mode="HTML"
        )
        return

    status_msg = await update.message.reply_text(
        "🔄 <b>Синхронизация топиков проектов...</b>", parse_mode="HTML"
    )

    if settings.project_threads_mode == "private":
        if not _is_private_chat(update):
            await status_msg.edit_text(
                "❌ <b>Режим приватных топиков</b>\n\n"
                "Выполните <code>/sync_threads</code> в личном чате с ботом.",
                parse_mode="HTML",
            )
            return
        target_chat_id = update.effective_chat.id
    else:
        if settings.project_threads_chat_id is None:
            await status_msg.edit_text(
                "❌ <b>Неверная конфигурация группового режима топиков</b>\n\n"
                "Сначала задайте <code>PROJECT_THREADS_CHAT_ID</code>.",
                parse_mode="HTML",
            )
            return
        if (
            not update.effective_chat
            or update.effective_chat.id != settings.project_threads_chat_id
        ):
            await status_msg.edit_text(
                "❌ <b>Групповой режим топиков</b>\n\n"
                "Выполните <code>/sync_threads</code> в настроенной группе топиков проектов.",
                parse_mode="HTML",
            )
            return
        target_chat_id = settings.project_threads_chat_id

    try:
        if not settings.projects_config_path:
            await status_msg.edit_text(
                "❌ <b>Неверная конфигурация топиков проектов</b>\n\n"
                "Укажите в <code>PROJECTS_CONFIG_PATH</code> путь к валидному YAML-файлу.",
                parse_mode="HTML",
            )
            if audit_logger:
                await audit_logger.log_command(user_id, "sync_threads", [], False)
            return

        registry = load_project_registry(
            config_path=settings.projects_config_path,
            approved_directory=settings.approved_directory,
        )
        manager.registry = registry
        context.bot_data["project_registry"] = registry

        result = await manager.sync_topics(context.bot, chat_id=target_chat_id)
        await status_msg.edit_text(
            "✅ <b>Синхронизация топиков завершена</b>\n\n"
            f"• Создано: <b>{result.created}</b>\n"
            f"• Повторно использовано: <b>{result.reused}</b>\n"
            f"• Переименовано: <b>{result.renamed}</b>\n"
            f"• Переоткрыто: <b>{result.reopened}</b>\n"
            f"• Закрыто: <b>{result.closed}</b>\n"
            f"• Деактивировано: <b>{result.deactivated}</b>\n"
            f"• Ошибок: <b>{result.failed}</b>",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_threads", [], True)
    except PrivateTopicsUnavailableError:
        await status_msg.edit_text(
            manager.private_topics_unavailable_message(),
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_threads", [], False)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>Ошибка синхронизации топиков</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_threads", [], False)


async def new_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new command - explicitly starts a fresh session, clearing previous context."""
    settings: Settings = context.bot_data["settings"]

    # Get current directory (default to approved directory)
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Track what was cleared for user feedback
    old_session_id = context.user_data.get("claude_session_id")

    # Clear existing session data - this is the explicit way to reset context
    context.user_data["claude_session_id"] = None
    context.user_data["session_started"] = True
    context.user_data["force_new_session"] = True

    cleared_info = ""
    if old_session_id:
        cleared_info = (
            f"\n🗑️ Previous session <code>{old_session_id[:8]}...</code> cleared."
        )

    keyboard = [
        [
            InlineKeyboardButton(
                "📝 Начать работу", callback_data="action:start_coding"
            ),
            InlineKeyboardButton(
                "📁 Сменить проект", callback_data="action:show_projects"
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 Быстрые действия", callback_data="action:quick_actions"
            ),
            InlineKeyboardButton("❓ Справка", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"🆕 <b>Новая сессия Claude Code</b>\n\n"
        f"📂 Рабочая директория: <code>{relative_path}/</code>{cleared_info}\n\n"
        f"Контекст очищен. Отправьте сообщение для начала работы "
        f"или воспользуйтесь кнопками ниже:",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def continue_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /continue command with optional prompt."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")

    # Parse optional prompt from command arguments
    # If no prompt provided, use a default to continue the conversation
    prompt = " ".join(context.args) if context.args else None
    default_prompt = "Please continue where we left off"

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        if not claude_integration:
            await update.message.reply_text(
                "❌ <b>Интеграция с Claude недоступна</b>\n\n"
                "Интеграция с Claude настроена неверно."
            )
            return

        # Check if there's an existing session in user context
        claude_session_id = context.user_data.get("claude_session_id")

        if claude_session_id:
            # We have a session in context, continue it directly
            status_msg = await update.message.reply_text(
                f"🔄 <b>Продолжение сессии</b>\n\n"
                f"ID сессии: <code>{claude_session_id[:8]}...</code>\n"
                f"Директория: <code>{current_dir.relative_to(settings.approved_directory)}/</code>\n\n"
                f"{'Обрабатываю ваше сообщение...' if prompt else 'Продолжаю с места остановки...'}",
                parse_mode="HTML",
            )

            # Continue with the existing session
            # Use default prompt if none provided (Claude CLI requires a prompt)
            claude_response = await claude_integration.run_command(
                prompt=prompt or default_prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=claude_session_id,
            )
        else:
            # No session in context, try to find the most recent session
            status_msg = await update.message.reply_text(
                "🔍 <b>Поиск последней сессии</b>\n\n"
                "Ищу вашу последнюю сессию в этой директории...",
                parse_mode="HTML",
            )

            # Use default prompt if none provided
            claude_response = await claude_integration.continue_session(
                user_id=user_id,
                working_directory=current_dir,
                prompt=prompt or default_prompt,
            )

        if claude_response:
            # Update session ID in context
            context.user_data["claude_session_id"] = claude_response.session_id

            # Delete status message and send response
            await status_msg.delete()

            # Format and send Claude's response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            for msg in formatted_messages:
                await update.message.reply_text(
                    msg.text,
                    parse_mode=msg.parse_mode,
                    reply_markup=msg.reply_markup,
                )

            # Log successful continue
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="continue",
                    args=context.args or [],
                    success=True,
                )

        else:
            # No session found to continue
            await status_msg.edit_text(
                "❌ <b>Сессия не найдена</b>\n\n"
                f"Недавняя сессия Claude в этой директории не найдена.\n"
                f"Директория: <code>{current_dir.relative_to(settings.approved_directory)}/</code>\n\n"
                f"<b>Что можно сделать:</b>\n"
                f"• Используйте <code>/new</code> для новой сессии\n"
                f"• Используйте <code>/status</code> для проверки сессий\n"
                f"• Перейдите в другую директорию через <code>/cd</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🆕 Новая сессия", callback_data="action:new_session"
                            ),
                            InlineKeyboardButton(
                                "📊 Статус", callback_data="action:status"
                            ),
                        ]
                    ]
                ),
            )

    except Exception as e:
        error_msg = str(e)
        logger.error("Error in continue command", error=error_msg, user_id=user_id)

        # Delete status message if it exists
        try:
            if "status_msg" in locals():
                await status_msg.delete()
        except Exception:
            pass

        # Send error response
        await update.message.reply_text(
            f"❌ <b>Ошибка продолжения сессии</b>\n\n"
            f"При попытке продолжить сессию произошла ошибка:\n\n"
            f"<code>{error_msg}</code>\n\n"
            f"<b>Рекомендации:</b>\n"
            f"• Попробуйте начать новую сессию через <code>/new</code>\n"
            f"• Проверьте статус сессии через <code>/status</code>\n"
            f"• Обратитесь к администратору, если проблема повторяется",
            parse_mode="HTML",
        )

        # Log failed continue
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="continue",
                args=context.args or [],
                success=False,
            )


async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ls command."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")

    # Get current directory
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        # List directory contents
        items = []
        directories = []
        files = []

        for item in sorted(current_dir.iterdir()):
            # Skip hidden files (starting with .)
            if item.name.startswith("."):
                continue

            # Escape HTML special characters in filenames
            safe_name = _escape_markdown(item.name)

            if item.is_dir():
                directories.append(f"📁 {safe_name}/")
            else:
                # Get file size
                try:
                    size = item.stat().st_size
                    size_str = _format_file_size(size)
                    files.append(f"📄 {safe_name} ({size_str})")
                except OSError:
                    files.append(f"📄 {safe_name}")

        # Combine directories first, then files
        items = directories + files

        # Format response
        relative_path = current_dir.relative_to(settings.approved_directory)
        if not items:
            message = f"📂 <code>{relative_path}/</code>\n\n<i>(empty directory)</i>"
        else:
            message = f"📂 <code>{relative_path}/</code>\n\n"

            # Limit items shown to prevent message being too long
            max_items = 50
            if len(items) > max_items:
                shown_items = items[:max_items]
                message += "\n".join(shown_items)
                message += f"\n\n<i>... and {len(items) - max_items} more items</i>"
            else:
                message += "\n".join(items)

        # Add navigation buttons if not at root
        keyboard = []
        if current_dir != settings.approved_directory:
            keyboard.append(
                [
                    InlineKeyboardButton("⬆️ Go Up", callback_data="cd:.."),
                    InlineKeyboardButton("🏠 Go to Root", callback_data="cd:/"),
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="action:refresh_ls"),
                InlineKeyboardButton(
                    "📁 Projects", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        await update.message.reply_text(
            message, parse_mode="HTML", reply_markup=reply_markup
        )

        # Log successful command
        if audit_logger:
            await audit_logger.log_command(user_id, "ls", [], True)

    except Exception as e:
        error_msg = f"❌ Ошибка при получении списка директорий: {str(e)}"
        await update.message.reply_text(error_msg)

        # Log failed command
        if audit_logger:
            await audit_logger.log_command(user_id, "ls", [], False)

        logger.error("Error in list_files command", error=str(e), user_id=user_id)


async def change_directory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cd command."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    security_validator: SecurityValidator = context.bot_data.get("security_validator")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")

    # Parse arguments
    if not context.args:
        await update.message.reply_text(
            "<b>Использование:</b> <code>/cd &lt;directory&gt;</code>\n\n"
            "<b>Примеры:</b>\n"
            "• <code>/cd myproject</code> - Перейти в поддиректорию\n"
            "• <code>/cd ..</code> - Перейти на уровень выше\n"
            "• <code>/cd /</code> - Перейти в корень разрешённой директории\n\n"
            "<b>Подсказки:</b>\n"
            "• Используйте <code>/ls</code> для просмотра доступных директорий\n"
            "• Используйте <code>/projects</code> для просмотра всех проектов",
            parse_mode="HTML",
        )
        return

    target_path = " ".join(context.args)
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    project_root = _get_thread_project_root(settings, context)
    directory_root = project_root or settings.approved_directory

    try:
        # Handle known navigation shortcuts first
        if target_path == "/":
            resolved_path = directory_root
        elif target_path == "..":
            resolved_path = current_dir.parent
            if not _is_within_root(resolved_path, directory_root):
                resolved_path = directory_root
        else:
            # Validate path using security validator
            if security_validator:
                valid, resolved_path, error = security_validator.validate_path(
                    target_path, current_dir
                )

                if not valid:
                    await update.message.reply_text(
                        f"❌ <b>Доступ запрещён</b>\n\n{error}"
                    )

                    # Log security violation
                    if audit_logger:
                        await audit_logger.log_security_violation(
                            user_id=user_id,
                            violation_type="path_traversal_attempt",
                            details=f"Attempted path: {target_path}",
                            severity="medium",
                        )
                    return
            else:
                resolved_path = current_dir / target_path
                resolved_path = resolved_path.resolve()

        if project_root and not _is_within_root(resolved_path, project_root):
            await update.message.reply_text(
                "❌ <b>Доступ запрещён</b>\n\n"
                "В режиме топиков навигация ограничена корнем текущего проекта.",
                parse_mode="HTML",
            )
            return

        # Check if directory exists and is actually a directory
        if not resolved_path.exists():
            await update.message.reply_text(
                f"❌ <b>Директория не найдена</b>\n\n<code>{target_path}</code> не существует."
            )
            return

        if not resolved_path.is_dir():
            await update.message.reply_text(
                f"❌ <b>Не является директорией</b>\n\n<code>{target_path}</code> не является директорией."
            )
            return

        # Update current directory in user data
        context.user_data["current_directory"] = resolved_path

        # Look up existing session for the new directory instead of clearing
        claude_integration: ClaudeIntegration = context.bot_data.get(
            "claude_integration"
        )
        resumed_session_info = ""
        if claude_integration:
            existing_session = await claude_integration._find_resumable_session(
                user_id, resolved_path
            )
            if existing_session:
                context.user_data["claude_session_id"] = existing_session.session_id
                resumed_session_info = (
                    f"\n🔄 Сессия возобновлена <code>{existing_session.session_id[:8]}...</code> "
                    f"({existing_session.message_count} сообщений)"
                )
            else:
                # No session for this directory - clear the current one
                context.user_data["claude_session_id"] = None
                resumed_session_info = (
                    "\n🆕 Существующая сессия не найдена. Отправьте сообщение для начала новой."
                )

        # Send confirmation
        relative_base = project_root or settings.approved_directory
        relative_path = resolved_path.relative_to(relative_base)
        relative_display = "/" if str(relative_path) == "." else f"{relative_path}/"
        await update.message.reply_text(
            f"✅ <b>Директория изменена</b>\n\n"
            f"📂 Текущая директория: <code>{relative_display}</code>"
            f"{resumed_session_info}",
            parse_mode="HTML",
        )

        # Log successful command
        if audit_logger:
            await audit_logger.log_command(user_id, "cd", [target_path], True)

    except Exception as e:
        error_msg = f"❌ <b>Ошибка при смене директории</b>\n\n{str(e)}"
        await update.message.reply_text(error_msg, parse_mode="HTML")

        # Log failed command
        if audit_logger:
            await audit_logger.log_command(user_id, "cd", [target_path], False)

        logger.error("Error in change_directory command", error=str(e), user_id=user_id)


async def print_working_directory(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /pwd command."""
    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    relative_path = current_dir.relative_to(settings.approved_directory)
    absolute_path = str(current_dir)

    # Add quick navigation buttons
    keyboard = [
        [
            InlineKeyboardButton("📁 List Files", callback_data="action:ls"),
            InlineKeyboardButton("📋 Projects", callback_data="action:show_projects"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"📍 <b>Текущая директория</b>\n\n"
        f"Относительный путь: <code>{relative_path}/</code>\n"
        f"Абсолютный путь: <code>{absolute_path}</code>",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def show_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /projects command."""
    settings: Settings = context.bot_data["settings"]

    try:
        if settings.enable_project_threads:
            registry = context.bot_data.get("project_registry")
            manager = context.bot_data.get("project_threads_manager")
            if manager and getattr(manager, "registry", None):
                registry = manager.registry
            if not registry:
                await update.message.reply_text(
                    "❌ <b>Реестр проектов не инициализирован.</b>",
                    parse_mode="HTML",
                )
                return

            projects = registry.list_enabled()
            if not projects:
                await update.message.reply_text(
                    "📁 <b>Проекты не найдены</b>\n\n"
                    "Активные проекты не найдены в конфигурации.",
                    parse_mode="HTML",
                )
                return

            project_list = "\n".join(
                [
                    f"• <b>{escape_html(p.name)}</b> "
                    f"(<code>{escape_html(p.slug)}</code>) "
                    f"→ <code>{escape_html(str(p.relative_path))}</code>"
                    for p in projects
                ]
            )

            await update.message.reply_text(
                f"📁 <b>Настроенные проекты</b>\n\n{project_list}",
                parse_mode="HTML",
            )
            return

        # Get directories in approved directory (these are "projects")
        projects = []
        for item in sorted(settings.approved_directory.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                projects.append(item.name)

        if not projects:
            await update.message.reply_text(
                "📁 <b>Проекты не найдены</b>\n\n"
                "В разрешённой директории не найдены поддиректории.\n"
                "Создайте несколько директорий для организации проектов!"
            )
            return

        # Create inline keyboard with project buttons
        keyboard = []
        for i in range(0, len(projects), 2):
            row = []
            for j in range(2):
                if i + j < len(projects):
                    project = projects[i + j]
                    row.append(
                        InlineKeyboardButton(
                            f"📁 {project}", callback_data=f"cd:{project}"
                        )
                    )
            keyboard.append(row)

        # Add navigation buttons
        keyboard.append(
            [
                InlineKeyboardButton("🏠 Go to Root", callback_data="cd:/"),
                InlineKeyboardButton(
                    "🔄 Refresh", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)

        project_list = "\n".join([f"• <code>{project}/</code>" for project in projects])

        await update.message.reply_text(
            f"📁 <b>Доступные проекты</b>\n\n"
            f"{project_list}\n\n"
            f"Нажмите на проект, чтобы перейти в него:",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка загрузки проектов: {str(e)}")
        logger.error("Error in show_projects command", error=str(e))


async def session_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]

    # Get session info
    claude_session_id = context.user_data.get("claude_session_id")
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Get rate limiter info if available
    rate_limiter = context.bot_data.get("rate_limiter")
    usage_info = ""
    if rate_limiter:
        try:
            user_status = rate_limiter.get_user_status(user_id)
            cost_usage = user_status.get("cost_usage", {})
            current_cost = cost_usage.get("current", 0.0)
            cost_limit = cost_usage.get("limit", settings.claude_max_cost_per_user)
            cost_percentage = (current_cost / cost_limit) * 100 if cost_limit > 0 else 0

            usage_info = f"💰 Usage: ${current_cost:.2f} / ${cost_limit:.2f} ({cost_percentage:.0f}%)\n"
        except Exception:
            usage_info = "💰 Usage: <i>Unable to retrieve</i>\n"

    # Check if there's a resumable session from the database
    resumable_info = ""
    if not claude_session_id:
        claude_integration: ClaudeIntegration = context.bot_data.get(
            "claude_integration"
        )
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                user_id, current_dir
            )
            if existing:
                resumable_info = (
                    f"🔄 Возможно продолжить: <code>{existing.session_id[:8]}...</code> "
                    f"({existing.message_count} сообщений)"
                )

    # Format status message
    status_lines = [
        "📊 <b>Статус сессии</b>",
        "",
        f"📂 Директория: <code>{relative_path}/</code>",
        f"🤖 Сессия Claude: {'✅ Активна' if claude_session_id else '❌ Нет'}",
        usage_info.rstrip(),
        f"🕐 Обновлено: {update.message.date.strftime('%H:%M:%S UTC')}",
    ]

    if claude_session_id:
        status_lines.append(f"🆔 ID сессии: <code>{claude_session_id[:8]}...</code>")
    elif resumable_info:
        status_lines.append(resumable_info)
        status_lines.append("💡 Сессия будет автоматически возобновлена при следующем сообщении")

    # Add action buttons
    keyboard = []
    if claude_session_id:
        keyboard.append(
            [
                InlineKeyboardButton("🔄 Continue", callback_data="action:continue"),
                InlineKeyboardButton(
                    "🆕 New Session", callback_data="action:new_session"
                ),
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🆕 Start Session", callback_data="action:new_session"
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton("📤 Export", callback_data="action:export"),
            InlineKeyboardButton("🔄 Refresh", callback_data="action:refresh_status"),
        ]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "\n".join(status_lines), parse_mode="HTML", reply_markup=reply_markup
    )


async def export_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /export command."""
    update.effective_user.id
    features = context.bot_data.get("features")

    # Check if session export is available
    session_exporter = features.get_session_export() if features else None

    if not session_exporter:
        await update.message.reply_text(
            "📤 <b>Экспорт сессии</b>\n\n"
            "Функция экспорта сессии недоступна.\n\n"
            "<b>Планируемые возможности:</b>\n"
            "• Экспорт истории разговора\n"
            "• Сохранение состояния сессии\n"
            "• Обмен разговорами\n"
            "• Резервные копии сессий"
        )
        return

    # Get current session
    claude_session_id = context.user_data.get("claude_session_id")

    if not claude_session_id:
        await update.message.reply_text(
            "❌ <b>Нет активной сессии</b>\n\n"
            "Нет активной сессии Claude для экспорта.\n\n"
            "<b>Что можно сделать:</b>\n"
            "• Начать новую сессию через <code>/new</code>\n"
            "• Продолжить существующую сессию через <code>/continue</code>\n"
            "• Проверить статус через <code>/status</code>"
        )
        return

    # Create export format selection keyboard
    keyboard = [
        [
            InlineKeyboardButton("📝 Markdown", callback_data="export:markdown"),
            InlineKeyboardButton("🌐 HTML", callback_data="export:html"),
        ],
        [
            InlineKeyboardButton("📋 JSON", callback_data="export:json"),
            InlineKeyboardButton("❌ Cancel", callback_data="export:cancel"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "📤 <b>Экспорт сессии</b>\n\n"
        f"Готово к экспорту: <code>{claude_session_id[:8]}...</code>\n\n"
        "<b>Выберите формат экспорта:</b>",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def end_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /end command to terminate the current session."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]

    # Check if there's an active session
    claude_session_id = context.user_data.get("claude_session_id")

    if not claude_session_id:
        await update.message.reply_text(
            "ℹ️ <b>Нет активной сессии</b>\n\n"
            "Нет активной сессии Claude для завершения.\n\n"
            "<b>Что можно сделать:</b>\n"
            "• Используйте <code>/new</code> для новой сессии\n"
            "• Используйте <code>/status</code> для проверки статуса\n"
            "• Отправьте любое сообщение для начала разговора"
        )
        return

    # Get current directory for display
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Clear session data
    context.user_data["claude_session_id"] = None
    context.user_data["session_started"] = False
    context.user_data["last_message"] = None

    # Create quick action buttons
    keyboard = [
        [
            InlineKeyboardButton("🆕 Новая сессия", callback_data="action:new_session"),
            InlineKeyboardButton(
                "📁 Сменить проект", callback_data="action:show_projects"
            ),
        ],
        [
            InlineKeyboardButton("📊 Статус", callback_data="action:status"),
            InlineKeyboardButton("❓ Справка", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "✅ <b>Сессия завершена</b>\n\n"
        f"Сессия Claude завершена.\n\n"
        f"<b>Текущий статус:</b>\n"
        f"• Директория: <code>{relative_path}/</code>\n"
        f"• Сессия: Нет\n"
        f"• Готово к новым командам\n\n"
        f"<b>Дальнейшие действия:</b>\n"
        f"• Начать новую сессию через <code>/new</code>\n"
        f"• Проверить статус через <code>/status</code>\n"
        f"• Отправьте любое сообщение для нового разговора",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )

    logger.info("Session ended by user", user_id=user_id, session_id=claude_session_id)


async def quick_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /actions command to show quick actions."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("quick_actions"):
        await update.message.reply_text(
            "❌ <b>Quick Actions Disabled</b>\n\n"
            "Quick actions feature is not enabled.\n"
            "Contact your administrator to enable this feature."
        )
        return

    # Get current directory
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        quick_action_manager = features.get_quick_actions()
        if not quick_action_manager:
            await update.message.reply_text(
                "❌ <b>Quick Actions Unavailable</b>\n\n"
                "Quick actions service is not available."
            )
            return

        # Get context-aware actions
        now = datetime.now(timezone.utc)
        actions = await quick_action_manager.get_suggestions(
            session=SessionModel(
                session_id="",  # ephemeral session for quick actions context
                user_id=user_id,
                project_path=str(current_dir),
                created_at=now,
                last_used=now,
            )
        )

        if not actions:
            await update.message.reply_text(
                "🤖 <b>No Actions Available</b>\n\n"
                "No quick actions are available for the current context.\n\n"
                "<b>Try:</b>\n"
                "• Navigating to a project directory with <code>/cd</code>\n"
                "• Creating some code files\n"
                "• Starting a Claude session with <code>/new</code>"
            )
            return

        # Create inline keyboard
        keyboard = quick_action_manager.create_inline_keyboard(actions, max_columns=2)

        relative_path = current_dir.relative_to(settings.approved_directory)
        await update.message.reply_text(
            f"⚡ <b>Quick Actions</b>\n\n"
            f"📂 Context: <code>{relative_path}/</code>\n\n"
            f"Select an action to execute:",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    except Exception as e:
        await update.message.reply_text(f"❌ <b>Error Loading Actions</b>\n\n{str(e)}")
        logger.error("Error in quick_actions command", error=str(e), user_id=user_id)


async def git_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /git command to show git repository information."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("git"):
        await update.message.reply_text(
            "❌ <b>Git Integration Disabled</b>\n\n"
            "Git integration feature is not enabled.\n"
            "Contact your administrator to enable this feature."
        )
        return

    # Get current directory
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        git_integration = features.get_git_integration()
        if not git_integration:
            await update.message.reply_text(
                "❌ <b>Git Integration Unavailable</b>\n\n"
                "Git integration service is not available."
            )
            return

        # Check if current directory is a git repository
        if not (current_dir / ".git").exists():
            await update.message.reply_text(
                f"📂 <b>Not a Git Repository</b>\n\n"
                f"Current directory <code>{current_dir.relative_to(settings.approved_directory)}/</code> is not a git repository.\n\n"
                f"<b>Options:</b>\n"
                f"• Navigate to a git repository with <code>/cd</code>\n"
                f"• Initialize a new repository (ask Claude to help)\n"
                f"• Clone an existing repository (ask Claude to help)"
            )
            return

        # Get git status
        git_status = await git_integration.get_status(current_dir)

        # Format status message
        relative_path = current_dir.relative_to(settings.approved_directory)
        status_message = "🔗 <b>Git Repository Status</b>\n\n"
        status_message += f"📂 Directory: <code>{relative_path}/</code>\n"
        status_message += f"🌿 Branch: <code>{git_status.branch}</code>\n"

        if git_status.ahead > 0:
            status_message += f"⬆️ Ahead: {git_status.ahead} commits\n"
        if git_status.behind > 0:
            status_message += f"⬇️ Behind: {git_status.behind} commits\n"

        # Show file changes
        if not git_status.is_clean:
            status_message += "\n<b>Changes:</b>\n"
            if git_status.modified:
                status_message += f"📝 Modified: {len(git_status.modified)} files\n"
            if git_status.added:
                status_message += f"➕ Added: {len(git_status.added)} files\n"
            if git_status.deleted:
                status_message += f"➖ Deleted: {len(git_status.deleted)} files\n"
            if git_status.untracked:
                status_message += f"❓ Untracked: {len(git_status.untracked)} files\n"
        else:
            status_message += "\n✅ Working directory clean\n"

        # Create action buttons
        keyboard = [
            [
                InlineKeyboardButton("📊 Show Diff", callback_data="git:diff"),
                InlineKeyboardButton("📜 Show Log", callback_data="git:log"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="git:status"),
                InlineKeyboardButton("📁 Files", callback_data="action:ls"),
            ],
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            status_message, parse_mode="HTML", reply_markup=reply_markup
        )

    except Exception as e:
        await update.message.reply_text(f"❌ <b>Git Error</b>\n\n{str(e)}")
        logger.error("Error in git_command", error=str(e), user_id=user_id)


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /restart command - gracefully restart the bot process.

    Sends a confirmation message then triggers SIGTERM so systemd
    (or any process manager with restart-on-exit) brings the bot back up.

    Auth: protected by the auth middleware (group -2) which raises
    ``ApplicationHandlerStop`` for unauthenticated users before any
    handler in group 10 runs.  No per-handler check is needed.
    """
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    user_id = update.effective_user.id

    await update.message.reply_text(
        "🔄 <b>Перезапуск бота…</b>\n\nСкоро вернусь.",
        parse_mode="HTML",
    )

    if audit_logger:
        await audit_logger.log_command(user_id, "restart", [], True)

    logger.info("Restart requested via /restart command", user_id=user_id)

    # SIGTERM triggers the existing graceful-shutdown handler in main.py;
    # systemd Restart=always will bring the process back up.
    os.kill(os.getpid(), signal.SIGTERM)


def _format_file_size(size: int) -> str:
    """Format file size in human-readable format."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return f"{size:.1f}TB"


def _escape_markdown(text: str) -> str:
    """Escape HTML-special characters in text for Telegram.

    Legacy name kept for compatibility with callers; actually escapes HTML.
    """
    return escape_html(text)
