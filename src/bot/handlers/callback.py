"""Handle inline keyboard callbacks."""

from pathlib import Path
from typing import Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
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


async def handle_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route callback queries to appropriate handlers."""
    query = update.callback_query
    await query.answer()  # Acknowledge the callback

    user_id = query.from_user.id
    data = query.data

    logger.info("Processing callback query", user_id=user_id, callback_data=data)

    try:
        # Parse callback data
        if ":" in data:
            action, param = data.split(":", 1)
        else:
            action, param = data, None

        # Route to appropriate handler
        handlers = {
            "cd": handle_cd_callback,
            "action": handle_action_callback,
            "confirm": handle_confirm_callback,
            "quick": handle_quick_action_callback,
            "followup": handle_followup_callback,
            "conversation": handle_conversation_callback,
            "git": handle_git_callback,
            "export": handle_export_callback,
        }

        handler = handlers.get(action)
        if handler:
            await handler(query, param, context)
        else:
            await query.edit_message_text(
                "❌ <b>Неизвестное действие</b>\n\n"
                "Это действие кнопки не распознано. "
                "Возможно, бот был обновлён после отправки этого сообщения.",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error(
            "Error handling callback query",
            error=str(e),
            user_id=user_id,
            callback_data=data,
        )

        try:
            await query.edit_message_text(
                "❌ <b>Ошибка обработки действия</b>\n\n"
                "При обработке вашего запроса произошла ошибка.\n"
                "Попробуйте ещё раз или используйте текстовые команды.",
                parse_mode="HTML",
            )
        except Exception:
            # If we can't edit the message, send a new one
            await query.message.reply_text(
                "❌ <b>Ошибка обработки действия</b>\n\n"
                "При обработке вашего запроса произошла ошибка.",
                parse_mode="HTML",
            )


async def handle_cd_callback(
    query, project_name: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle directory change from inline keyboard."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    security_validator: SecurityValidator = context.bot_data.get("security_validator")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")

    try:
        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        project_root = _get_thread_project_root(settings, context)
        directory_root = project_root or settings.approved_directory

        # Handle special paths
        if project_name == "/":
            new_path = directory_root
        elif project_name == "..":
            new_path = current_dir.parent
            if not _is_within_root(new_path, directory_root):
                new_path = directory_root
        else:
            if project_root:
                new_path = current_dir / project_name
            else:
                new_path = settings.approved_directory / project_name

        # Validate path if security validator is available
        if security_validator:
            # Pass the absolute path for validation
            valid, resolved_path, error = security_validator.validate_path(
                str(new_path), settings.approved_directory
            )
            if not valid:
                await query.edit_message_text(
                    f"❌ <b>Доступ запрещён</b>\n\n{escape_html(error)}",
                    parse_mode="HTML",
                )
                return
            # Use the validated path
            new_path = resolved_path

        if project_root and not _is_within_root(new_path, project_root):
            await query.edit_message_text(
                "❌ <b>Доступ запрещён</b>\n\n"
                "В режиме топиков навигация ограничена корнем текущего проекта.",
                parse_mode="HTML",
            )
            return

        # Check if directory exists
        if not new_path.exists() or not new_path.is_dir():
            await query.edit_message_text(
                f"❌ <b>Директория не найдена</b>\n\n"
                f"Директория <code>{escape_html(project_name)}</code> больше не существует или недоступна.",
                parse_mode="HTML",
            )
            return

        # Update directory and resume session for that directory when available
        context.user_data["current_directory"] = new_path

        resumed_session_info = ""
        if claude_integration:
            existing_session = await claude_integration._find_resumable_session(
                user_id, new_path
            )
            if existing_session:
                context.user_data["claude_session_id"] = existing_session.session_id
                resumed_session_info = (
                    f"\n🔄 Сессия возобновлена <code>{escape_html(existing_session.session_id[:8])}...</code> "
                    f"({existing_session.message_count} сообщений)"
                )
            else:
                context.user_data["claude_session_id"] = None
                resumed_session_info = (
                    "\n🆕 Существующая сессия не найдена. Отправьте сообщение для начала новой."
                )
        else:
            context.user_data["claude_session_id"] = None
            resumed_session_info = "\n🆕 Отправьте сообщение для начала новой сессии."

        # Send confirmation with new directory info
        relative_base = project_root or settings.approved_directory
        relative_path = new_path.relative_to(relative_base)
        relative_display = "/" if str(relative_path) == "." else f"{relative_path}/"

        # Add navigation buttons
        keyboard = [
            [
                InlineKeyboardButton("📁 List Files", callback_data="action:ls"),
                InlineKeyboardButton(
                    "🆕 New Session", callback_data="action:new_session"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 Projects", callback_data="action:show_projects"
                ),
                InlineKeyboardButton("📊 Status", callback_data="action:status"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"✅ <b>Директория изменена</b>\n\n"
            f"📂 Текущая директория: <code>{escape_html(str(relative_display))}</code>"
            f"{resumed_session_info}",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

        # Log successful directory change
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="cd", args=[project_name], success=True
            )

    except Exception as e:
        await query.edit_message_text(
            f"❌ <b>Ошибка при смене директории</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )

        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="cd", args=[project_name], success=False
            )


async def handle_action_callback(
    query, action_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle general action callbacks."""
    actions = {
        "help": _handle_help_action,
        "show_projects": _handle_show_projects_action,
        "new_session": _handle_new_session_action,
        "continue": _handle_continue_action,
        "end_session": _handle_end_session_action,
        "status": _handle_status_action,
        "ls": _handle_ls_action,
        "start_coding": _handle_start_coding_action,
        "quick_actions": _handle_quick_actions_action,
        "refresh_status": _handle_refresh_status_action,
        "refresh_ls": _handle_refresh_ls_action,
        "export": _handle_export_action,
    }

    handler = actions.get(action_type)
    if handler:
        await handler(query, context)
    else:
        await query.edit_message_text(
            f"❌ <b>Неизвестное действие: {escape_html(action_type)}</b>\n\n"
            "Это действие ещё не реализовано.",
            parse_mode="HTML",
        )


async def handle_confirm_callback(
    query, confirmation_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle confirmation dialogs."""
    if confirmation_type == "yes":
        await query.edit_message_text(
            "✅ <b>Подтверждено</b>\n\nДействие будет выполнено.",
            parse_mode="HTML",
        )
    elif confirmation_type == "no":
        await query.edit_message_text(
            "❌ <b>Отменено</b>\n\nДействие отменено.",
            parse_mode="HTML",
        )
    else:
        await query.edit_message_text(
            "❓ <b>Неизвестный ответ подтверждения</b>",
            parse_mode="HTML",
        )


# Action handlers


async def _handle_help_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle help action."""
    help_text = (
        "🤖 <b>Краткая справка</b>\n\n"
        "<b>Навигация:</b>\n"
        "• <code>/ls</code> - Список файлов\n"
        "• <code>/cd &lt;dir&gt;</code> - Сменить директорию\n"
        "• <code>/projects</code> - Показать проекты\n\n"
        "<b>Сессии:</b>\n"
        "• <code>/new</code> - Новая сессия Claude\n"
        "• <code>/status</code> - Статус сессии\n\n"
        "<b>Советы:</b>\n"
        "• Отправьте любой текст для работы с Claude\n"
        "• Загружайте файлы для проверки кода\n"
        "• Используйте кнопки для быстрых действий\n\n"
        "Используйте <code>/help</code> для подробной справки."
    )

    keyboard = [
        [
            InlineKeyboardButton("📖 Full Help", callback_data="action:full_help"),
            InlineKeyboardButton("🏠 Main Menu", callback_data="action:main_menu"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        help_text, parse_mode="HTML", reply_markup=reply_markup
    )


async def _handle_show_projects_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle show projects action."""
    settings: Settings = context.bot_data["settings"]

    try:
        if settings.enable_project_threads:
            registry = context.bot_data.get("project_registry")
            if not registry:
                await query.edit_message_text(
                    "❌ <b>Реестр проектов не инициализирован.</b>",
                    parse_mode="HTML",
                )
                return

            projects = registry.list_enabled()
            if not projects:
                await query.edit_message_text(
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

            await query.edit_message_text(
                f"📁 <b>Настроенные проекты</b>\n\n{project_list}",
                parse_mode="HTML",
            )
            return

        # Get directories in approved directory
        projects = []
        for item in sorted(settings.approved_directory.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                projects.append(item.name)

        if not projects:
            await query.edit_message_text(
                "📁 <b>Проекты не найдены</b>\n\n"
                "В разрешённой директории не найдены поддиректории.\n"
                "Создайте несколько директорий для организации проектов!",
                parse_mode="HTML",
            )
            return

        # Create project buttons
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
                InlineKeyboardButton("🏠 Root", callback_data="cd:/"),
                InlineKeyboardButton(
                    "🔄 Refresh", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)
        project_list = "\n".join(
            [f"• <code>{escape_html(project)}/</code>" for project in projects]
        )

        await query.edit_message_text(
            f"📁 <b>Доступные проекты</b>\n\n"
            f"{project_list}\n\n"
            f"Нажмите на проект, чтобы перейти в него:",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка загрузки проектов: {str(e)}")


async def _handle_new_session_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle new session action."""
    settings: Settings = context.bot_data["settings"]

    # Clear session
    context.user_data["claude_session_id"] = None
    context.user_data["session_started"] = True

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    keyboard = [
        [
            InlineKeyboardButton(
                "📝 Start Coding", callback_data="action:start_coding"
            ),
            InlineKeyboardButton(
                "📁 Change Project", callback_data="action:show_projects"
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 Quick Actions", callback_data="action:quick_actions"
            ),
            InlineKeyboardButton("❓ Help", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"🆕 <b>Новая сессия Claude Code</b>\n\n"
        f"📂 Рабочая директория: <code>{escape_html(str(relative_path))}/</code>\n\n"
        f"Готов помочь с кодом! Отправьте сообщение для начала работы:",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def _handle_end_session_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle end session action."""
    settings: Settings = context.bot_data["settings"]

    # Check if there's an active session
    claude_session_id = context.user_data.get("claude_session_id")

    if not claude_session_id:
        await query.edit_message_text(
            "ℹ️ <b>Нет активной сессии</b>\n\n"
            "Нет активной сессии Claude для завершения.\n\n"
            "<b>Что можно сделать:</b>\n"
            "• Используйте кнопку ниже для новой сессии\n"
            "• Проверьте статус сессии\n"
            "• Отправьте любое сообщение для начала разговора",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🆕 New Session", callback_data="action:new_session"
                        )
                    ],
                    [InlineKeyboardButton("📊 Status", callback_data="action:status")],
                ]
            ),
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
            InlineKeyboardButton("🆕 New Session", callback_data="action:new_session"),
            InlineKeyboardButton(
                "📁 Change Project", callback_data="action:show_projects"
            ),
        ],
        [
            InlineKeyboardButton("📊 Status", callback_data="action:status"),
            InlineKeyboardButton("❓ Help", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "✅ <b>Сессия завершена</b>\n\n"
        f"Сессия Claude завершена.\n\n"
        f"<b>Текущий статус:</b>\n"
        f"• Директория: <code>{escape_html(str(relative_path))}/</code>\n"
        f"• Сессия: Нет\n"
        f"• Готово к новым командам\n\n"
        f"<b>Дальнейшие действия:</b>\n"
        f"• Начать новую сессию\n"
        f"• Проверить статус\n"
        f"• Отправить любое сообщение для нового разговора",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def _handle_continue_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle continue session action."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        if not claude_integration:
            await query.edit_message_text(
                "❌ <b>Интеграция с Claude недоступна</b>\n\n"
                "Интеграция с Claude настроена неверно.",
                parse_mode="HTML",
            )
            return

        # Check if there's an existing session in user context
        claude_session_id = context.user_data.get("claude_session_id")

        if claude_session_id:
            # Continue with the existing session (no prompt = use --continue)
            await query.edit_message_text(
                f"🔄 <b>Продолжение сессии</b>\n\n"
                f"ID сессии: <code>{escape_html(claude_session_id[:8])}...</code>\n"
                f"Директория: <code>{escape_html(str(current_dir.relative_to(settings.approved_directory)))}/</code>\n\n"
                f"Продолжаю с места остановки...",
                parse_mode="HTML",
            )

            claude_response = await claude_integration.run_command(
                prompt="",  # Empty prompt triggers --continue
                working_directory=current_dir,
                user_id=user_id,
                session_id=claude_session_id,
            )
        else:
            # No session in context, try to find the most recent session
            await query.edit_message_text(
                "🔍 <b>Поиск последней сессии</b>\n\n"
                "Ищу вашу последнюю сессию в этой директории...",
                parse_mode="HTML",
            )

            claude_response = await claude_integration.continue_session(
                user_id=user_id,
                working_directory=current_dir,
                prompt=None,  # No prompt = use --continue
            )

        if claude_response:
            # Update session ID in context
            context.user_data["claude_session_id"] = claude_response.session_id

            # Send Claude's response
            await query.message.reply_text(
                f"✅ <b>Сессия продолжена</b>\n\n"
                f"{escape_html(claude_response.content[:500])}{'...' if len(claude_response.content) > 500 else ''}",
                parse_mode="HTML",
            )
        else:
            # No session found to continue
            await query.edit_message_text(
                "❌ <b>Сессия не найдена</b>\n\n"
                f"Недавняя сессия Claude в этой директории не найдена.\n"
                f"Директория: <code>{escape_html(str(current_dir.relative_to(settings.approved_directory)))}/</code>\n\n"
                f"<b>Что можно сделать:</b>\n"
                f"• Используйте кнопку ниже для новой сессии\n"
                f"• Проверьте статус сессии\n"
                f"• Перейдите в другую директорию",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🆕 New Session", callback_data="action:new_session"
                            ),
                            InlineKeyboardButton(
                                "📊 Status", callback_data="action:status"
                            ),
                        ]
                    ]
                ),
            )

    except Exception as e:
        logger.error("Error in continue action", error=str(e), user_id=user_id)
        await query.edit_message_text(
            f"❌ <b>Ошибка продолжения сессии</b>\n\n"
            f"Произошла ошибка: <code>{escape_html(str(e))}</code>\n\n"
            f"Попробуйте начать новую сессию.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🆕 New Session", callback_data="action:new_session"
                        )
                    ]
                ]
            ),
        )


async def _handle_status_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle status action."""
    # This essentially duplicates the /status command functionality
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]

    claude_session_id = context.user_data.get("claude_session_id")
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Get usage info if rate limiter is available
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

    status_lines = [
        "📊 <b>Статус сессии</b>",
        "",
        f"📂 Директория: <code>{escape_html(str(relative_path))}/</code>",
        f"🤖 Сессия Claude: {'✅ Активна' if claude_session_id else '❌ Нет'}",
        usage_info.rstrip(),
    ]

    if claude_session_id:
        status_lines.append(
            f"🆔 ID сессии: <code>{escape_html(claude_session_id[:8])}...</code>"
        )

    # Add action buttons
    keyboard = []
    if claude_session_id:
        keyboard.append(
            [
                InlineKeyboardButton("🔄 Continue", callback_data="action:continue"),
                InlineKeyboardButton(
                    "🛑 End Session", callback_data="action:end_session"
                ),
            ]
        )
        keyboard.append(
            [
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
            InlineKeyboardButton("🔄 Refresh", callback_data="action:refresh_status"),
            InlineKeyboardButton("📁 Projects", callback_data="action:show_projects"),
        ]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "\n".join(status_lines), parse_mode="HTML", reply_markup=reply_markup
    )


async def _handle_ls_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ls action."""
    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        # List directory contents (similar to /ls command)
        items = []
        directories = []
        files = []

        for item in sorted(current_dir.iterdir()):
            if item.name.startswith("."):
                continue

            # Escape markdown special characters in filenames
            safe_name = _escape_markdown(item.name)

            if item.is_dir():
                directories.append(f"📁 {safe_name}/")
            else:
                try:
                    size = item.stat().st_size
                    size_str = _format_file_size(size)
                    files.append(f"📄 {safe_name} ({size_str})")
                except OSError:
                    files.append(f"📄 {safe_name}")

        items = directories + files
        relative_path = current_dir.relative_to(settings.approved_directory)

        if not items:
            message = f"📂 <code>{escape_html(str(relative_path))}/</code>\n\n<i>(empty directory)</i>"
        else:
            message = f"📂 <code>{escape_html(str(relative_path))}/</code>\n\n"
            max_items = 30  # Limit for inline display
            if len(items) > max_items:
                shown_items = items[:max_items]
                message += "\n".join(shown_items)
                message += f"\n\n<i>... and {len(items) - max_items} more items</i>"
            else:
                message += "\n".join(items)

        # Add buttons
        keyboard = []
        if current_dir != settings.approved_directory:
            keyboard.append(
                [
                    InlineKeyboardButton("⬆️ Go Up", callback_data="cd:.."),
                    InlineKeyboardButton("🏠 Root", callback_data="cd:/"),
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="action:refresh_ls"),
                InlineKeyboardButton(
                    "📋 Projects", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            message, parse_mode="HTML", reply_markup=reply_markup
        )

    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка при получении списка директорий: {str(e)}")


async def _handle_start_coding_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle start coding action."""
    await query.edit_message_text(
        "🚀 <b>Готов к работе!</b>\n\n"
        "Отправьте любое сообщение для работы с Claude:\n\n"
        "<b>Примеры:</b>\n"
        '• <i>"Создай Python-скрипт, который..."</i>\n'
        '• <i>"Помоги отладить этот код..."</i>\n'
        '• <i>"Объясни, как работает этот файл..."</i>\n'
        "• Загрузите файл для проверки\n\n"
        "Я здесь, чтобы помочь с вашим кодом!",
        parse_mode="HTML",
    )


async def _handle_quick_actions_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle quick actions menu."""
    keyboard = [
        [
            InlineKeyboardButton("🧪 Run Tests", callback_data="quick:test"),
            InlineKeyboardButton("📦 Install Deps", callback_data="quick:install"),
        ],
        [
            InlineKeyboardButton("🎨 Format Code", callback_data="quick:format"),
            InlineKeyboardButton("🔍 Find TODOs", callback_data="quick:find_todos"),
        ],
        [
            InlineKeyboardButton("🔨 Build", callback_data="quick:build"),
            InlineKeyboardButton("🚀 Start Server", callback_data="quick:start"),
        ],
        [
            InlineKeyboardButton("📊 Git Status", callback_data="quick:git_status"),
            InlineKeyboardButton("🔧 Lint Code", callback_data="quick:lint"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="action:new_session")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🛠️ <b>Быстрые действия</b>\n\n"
        "Выберите типичную задачу разработки:\n\n"
        "<i>Примечание: Будут полностью доступны после завершения интеграции Claude Code.</i>",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def _handle_refresh_status_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle refresh status action."""
    await _handle_status_action(query, context)


async def _handle_refresh_ls_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle refresh ls action."""
    await _handle_ls_action(query, context)


async def _handle_export_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle export action."""
    await query.edit_message_text(
        "📤 <b>Экспорт сессии</b>\n\n"
        "Функция экспорта будет доступна после реализации слоя хранения.\n\n"
        "<b>Планируемые возможности:</b>\n"
        "• Экспорт истории разговора\n"
        "• Сохранение состояния сессии\n"
        "• Обмен разговорами\n"
        "• Резервные копии сессий\n\n"
        "<i>Появится в следующей фазе разработки!</i>",
        parse_mode="HTML",
    )


async def handle_quick_action_callback(
    query, action_id: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle quick action callbacks."""
    user_id = query.from_user.id

    # Get quick actions manager from bot data if available
    quick_actions = context.bot_data.get("quick_actions")

    if not quick_actions:
        await query.edit_message_text(
            "❌ <b>Быстрые действия недоступны</b>\n\n"
            "Функция быстрых действий не доступна.",
            parse_mode="HTML",
        )
        return

    # Get Claude integration
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")
    if not claude_integration:
        await query.edit_message_text(
            "❌ <b>Интеграция с Claude недоступна</b>\n\n"
            "Интеграция с Claude настроена неверно.",
            parse_mode="HTML",
        )
        return

    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        # Get the action from the manager
        action = quick_actions.actions.get(action_id)
        if not action:
            await query.edit_message_text(
                f"❌ <b>Действие не найдено</b>\n\n"
                f"Быстрое действие '{escape_html(action_id)}' недоступно.",
                parse_mode="HTML",
            )
            return

        # Execute the action
        await query.edit_message_text(
            f"🚀 <b>Выполняю {action.icon} {escape_html(action.name)}</b>\n\n"
            f"Запускаю действие в директории: <code>{escape_html(str(current_dir.relative_to(settings.approved_directory)))}/</code>\n\n"
            f"Подождите...",
            parse_mode="HTML",
        )

        # Run the action through Claude
        claude_response = await claude_integration.run_command(
            prompt=action.prompt, working_directory=current_dir, user_id=user_id
        )

        if claude_response:
            # Format and send the response
            response_text = escape_html(claude_response.content)
            if len(response_text) > 4000:
                response_text = (
                    response_text[:4000] + "...\n\n<i>(Ответ обрезан)</i>"
                )

            await query.message.reply_text(
                f"✅ <b>{action.icon} {escape_html(action.name)} завершено</b>\n\n{response_text}",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                f"❌ <b>Ошибка выполнения действия</b>\n\n"
                f"Не удалось выполнить {escape_html(action.name)}. Попробуйте ещё раз.",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error("Quick action execution failed", error=str(e), user_id=user_id)
        await query.edit_message_text(
            f"❌ <b>Ошибка действия</b>\n\n"
            f"При выполнении {escape_html(action_id)} произошла ошибка: {escape_html(str(e))}",
            parse_mode="HTML",
        )


async def handle_followup_callback(
    query, suggestion_hash: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle follow-up suggestion callbacks."""
    user_id = query.from_user.id

    # Get conversation enhancer from bot data if available
    conversation_enhancer = context.bot_data.get("conversation_enhancer")

    if not conversation_enhancer:
        await query.edit_message_text(
            "❌ <b>Уточнение недоступно</b>\n\n"
            "Функции улучшения разговора не доступны.",
            parse_mode="HTML",
        )
        return

    try:
        # Get stored suggestions (this would need to be implemented in the enhancer)
        # For now, we'll provide a generic response
        await query.edit_message_text(
            "💡 <b>Уточнение выбрано</b>\n\n"
            "Эта функция будет реализована после полной интеграции "
            "системы улучшения разговора с обработчиком сообщений.\n\n"
            "<b>Текущий статус:</b>\n"
            "• Уточнение получено ✅\n"
            "• Интеграция ожидается 🔄\n\n"
            "<i>Вы можете продолжить разговор, отправив новое сообщение.</i>",
            parse_mode="HTML",
        )

        logger.info(
            "Follow-up suggestion selected",
            user_id=user_id,
            suggestion_hash=suggestion_hash,
        )

    except Exception as e:
        logger.error(
            "Error handling follow-up callback",
            error=str(e),
            user_id=user_id,
            suggestion_hash=suggestion_hash,
        )

        await query.edit_message_text(
            "❌ <b>Ошибка обработки уточнения</b>\n\n"
            "При обработке вашего уточнения произошла ошибка.",
            parse_mode="HTML",
        )


async def handle_conversation_callback(
    query, action_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle conversation control callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]

    if action_type == "continue":
        # Remove suggestion buttons and show continue message
        await query.edit_message_text(
            "✅ <b>Разговор продолжается</b>\n\n"
            "Отправьте следующее сообщение для продолжения работы!\n\n"
            "Готов помочь с:\n"
            "• Проверкой и отладкой кода\n"
            "• Реализацией функций\n"
            "• Архитектурными решениями\n"
            "• Тестированием и оптимизацией\n"
            "• Документацией\n\n"
            "<i>Просто напишите запрос или загрузите файлы.</i>",
            parse_mode="HTML",
        )

    elif action_type == "end":
        # End the current session
        conversation_enhancer = context.bot_data.get("conversation_enhancer")
        if conversation_enhancer:
            conversation_enhancer.clear_context(user_id)

        # Clear session data
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = False

        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        relative_path = current_dir.relative_to(settings.approved_directory)

        # Create quick action buttons
        keyboard = [
            [
                InlineKeyboardButton(
                    "🆕 New Session", callback_data="action:new_session"
                ),
                InlineKeyboardButton(
                    "📁 Change Project", callback_data="action:show_projects"
                ),
            ],
            [
                InlineKeyboardButton("📊 Status", callback_data="action:status"),
                InlineKeyboardButton("❓ Help", callback_data="action:help"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "✅ <b>Разговор завершён</b>\n\n"
            f"Сессия Claude завершена.\n\n"
            f"<b>Текущий статус:</b>\n"
            f"• Директория: <code>{escape_html(str(relative_path))}/</code>\n"
            f"• Сессия: Нет\n"
            f"• Готово к новым командам\n\n"
            f"<b>Дальнейшие действия:</b>\n"
            f"• Начать новую сессию\n"
            f"• Проверить статус\n"
            f"• Отправить любое сообщение для нового разговора",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

        logger.info("Conversation ended via callback", user_id=user_id)

    else:
        await query.edit_message_text(
            f"❌ <b>Неизвестное действие разговора: {escape_html(action_type)}</b>\n\n"
            "Это действие разговора не распознано.",
            parse_mode="HTML",
        )


async def handle_git_callback(
    query, git_action: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle git-related callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("git"):
        await query.edit_message_text(
            "❌ <b>Git-интеграция отключена</b>\n\n"
            "Функция интеграции с Git не включена.",
            parse_mode="HTML",
        )
        return

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        git_integration = features.get_git_integration()
        if not git_integration:
            await query.edit_message_text(
                "❌ <b>Git-интеграция недоступна</b>\n\n"
                "Сервис интеграции с Git не доступен.",
                parse_mode="HTML",
            )
            return

        if git_action == "status":
            # Refresh git status
            git_status = await git_integration.get_status(current_dir)
            status_message = git_integration.format_status(git_status)

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

            await query.edit_message_text(
                status_message, parse_mode="HTML", reply_markup=reply_markup
            )

        elif git_action == "diff":
            # Show git diff
            diff_output = await git_integration.get_diff(current_dir)

            if not diff_output.strip():
                diff_message = "📊 <b>Git Diff</b>\n\n<i>Изменений нет.</i>"
            else:
                # Clean up diff output for Telegram
                # Remove emoji symbols that interfere with parsing
                clean_diff = (
                    diff_output.replace("➕", "+").replace("➖", "-").replace("📍", "@")
                )

                # Limit diff output (leave room for header + HTML tags within
                # Telegram's 4096-char message limit)
                max_length = 3500
                if len(clean_diff) > max_length:
                    clean_diff = (
                        clean_diff[:max_length] + "\n\n... вывод обрезан ..."
                    )

                escaped_diff = escape_html(clean_diff)
                diff_message = (
                    f"📊 <b>Git Diff</b>\n\n<pre><code>{escaped_diff}</code></pre>"
                )

            keyboard = [
                [
                    InlineKeyboardButton("📜 Show Log", callback_data="git:log"),
                    InlineKeyboardButton("📊 Status", callback_data="git:status"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                diff_message, parse_mode="HTML", reply_markup=reply_markup
            )

        elif git_action == "log":
            # Show git log
            commits = await git_integration.get_file_history(current_dir, ".")

            if not commits:
                log_message = "📜 <b>Git Log</b>\n\n<i>Коммиты не найдены.</i>"
            else:
                log_message = "📜 <b>Git Log</b>\n\n"
                for commit in commits[:10]:  # Show last 10 commits
                    short_hash = commit.hash[:7]
                    short_message = escape_html(commit.message[:60])
                    if len(commit.message) > 60:
                        short_message += "..."
                    log_message += f"• <code>{short_hash}</code> {short_message}\n"

            keyboard = [
                [
                    InlineKeyboardButton("📊 Show Diff", callback_data="git:diff"),
                    InlineKeyboardButton("📊 Status", callback_data="git:status"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                log_message, parse_mode="HTML", reply_markup=reply_markup
            )

        else:
            await query.edit_message_text(
                f"❌ <b>Неизвестное Git-действие: {escape_html(git_action)}</b>\n\n"
                "Это действие Git не распознано.",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error(
            "Error in git callback",
            error=str(e),
            git_action=git_action,
            user_id=user_id,
        )
        await query.edit_message_text(
            f"❌ <b>Git Error</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )


async def handle_export_callback(
    query, export_format: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle export format selection callbacks."""
    user_id = query.from_user.id
    features = context.bot_data.get("features")

    if export_format == "cancel":
        await query.edit_message_text(
            "📤 <b>Экспорт отменён</b>\n\n" "Экспорт сессии отменён.",
            parse_mode="HTML",
        )
        return

    session_exporter = features.get_session_export() if features else None
    if not session_exporter:
        await query.edit_message_text(
            "❌ <b>Экспорт недоступен</b>\n\n"
            "Сервис экспорта сессий не доступен.",
            parse_mode="HTML",
        )
        return

    # Get current session
    claude_session_id = context.user_data.get("claude_session_id")
    if not claude_session_id:
        await query.edit_message_text(
            "❌ <b>Нет активной сессии</b>\n\n" "Нет активной сессии для экспорта.",
            parse_mode="HTML",
        )
        return

    try:
        # Show processing message
        await query.edit_message_text(
            f"📤 <b>Экспорт сессии</b>\n\n"
            f"Генерирую {escape_html(export_format.upper())} экспорт...",
            parse_mode="HTML",
        )

        # Export session
        exported_session = await session_exporter.export_session(
            claude_session_id, export_format
        )

        # Send the exported file
        from io import BytesIO

        file_bytes = BytesIO(exported_session.content.encode("utf-8"))
        file_bytes.name = exported_session.filename

        await query.message.reply_document(
            document=file_bytes,
            filename=exported_session.filename,
            caption=(
                f"📤 <b>Экспорт сессии завершён</b>\n\n"
                f"Формат: {escape_html(exported_session.format.upper())}\n"
                f"Размер: {exported_session.size_bytes:,} байт\n"
                f"Создан: {exported_session.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            parse_mode="HTML",
        )

        # Update the original message
        await query.edit_message_text(
            f"✅ <b>Экспорт завершён</b>\n\n"
            f"Сессия экспортирована как {escape_html(exported_session.filename)}.\n"
            f"Проверьте файл выше для полной истории разговора.",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(
            "Export failed", error=str(e), user_id=user_id, format=export_format
        )
        await query.edit_message_text(
            f"❌ <b>Ошибка экспорта</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )


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
