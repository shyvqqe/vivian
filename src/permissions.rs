//! Discord permission identifiers presented as actionable, human-readable names.

use poise::serenity_prelude as serenity;

const PERMISSIONS: [(serenity::Permissions, &str); 20] = [
    (
        serenity::Permissions::ADMINISTRATOR,
        "Administrator (Администратор)",
    ),
    (
        serenity::Permissions::VIEW_AUDIT_LOG,
        "View Audit Log (Просмотр журнала аудита)",
    ),
    (
        serenity::Permissions::MANAGE_GUILD,
        "Manage Server (Управлять сервером)",
    ),
    (
        serenity::Permissions::MANAGE_ROLES,
        "Manage Roles (Управлять ролями)",
    ),
    (
        serenity::Permissions::MANAGE_CHANNELS,
        "Manage Channels (Управлять каналами)",
    ),
    (
        serenity::Permissions::KICK_MEMBERS,
        "Kick Members (Исключать участников)",
    ),
    (
        serenity::Permissions::BAN_MEMBERS,
        "Ban Members (Блокировать участников)",
    ),
    (
        serenity::Permissions::MODERATE_MEMBERS,
        "Moderate Members (Выдавать тайм-ауты)",
    ),
    (
        serenity::Permissions::MANAGE_MESSAGES,
        "Manage Messages (Управлять сообщениями)",
    ),
    (
        serenity::Permissions::SEND_MESSAGES,
        "Send Messages (Отправлять сообщения)",
    ),
    (
        serenity::Permissions::SEND_MESSAGES_IN_THREADS,
        "Send Messages in Threads (Отправлять сообщения в ветках)",
    ),
    (
        serenity::Permissions::READ_MESSAGE_HISTORY,
        "Read Message History (Читать историю сообщений)",
    ),
    (
        serenity::Permissions::VIEW_CHANNEL,
        "View Channels (Просматривать каналы)",
    ),
    (
        serenity::Permissions::ATTACH_FILES,
        "Attach Files (Прикреплять файлы)",
    ),
    (
        serenity::Permissions::EMBED_LINKS,
        "Embed Links (Встраивать ссылки)",
    ),
    (
        serenity::Permissions::CONNECT,
        "Connect (Подключаться к голосовым каналам)",
    ),
    (
        serenity::Permissions::SPEAK,
        "Speak (Говорить в голосовых каналах)",
    ),
    (
        serenity::Permissions::MOVE_MEMBERS,
        "Move Members (Перемещать участников)",
    ),
    (
        serenity::Permissions::CREATE_EVENTS,
        "Create Events (Создавать события)",
    ),
    (
        serenity::Permissions::MANAGE_EVENTS,
        "Manage Events (Управлять событиями)",
    ),
];

pub fn describe(missing: serenity::Permissions) -> String {
    let names = PERMISSIONS
        .iter()
        .filter(|(permission, _)| missing.contains(*permission))
        .map(|(_, label)| *label)
        .collect::<Vec<_>>();
    if names.is_empty() {
        "permissions for this command (check server and channel overrides)".to_owned()
    } else {
        names.join(", ")
    }
}
