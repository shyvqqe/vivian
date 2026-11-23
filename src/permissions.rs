//! Discord permission identifiers presented as actionable, human-readable names.

use poise::serenity_prelude as serenity;

const PERMISSIONS: [(serenity::Permissions, &str); 19] = [
    (
        serenity::Permissions::ADMINISTRATOR,
        "Administrator (Администратор)",
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

/// Exact permission expected from Vivian when a Discord action is rejected.
/// The command checks remain at the command level so ordinary commands do not
/// inherit unrelated moderation permissions.
pub fn bot_requirement_for_command(command: &str) -> Option<&'static str> {
    match command {
        "timeout" | "untimeout" => Some("Moderate Members (Выдавать тайм-ауты)"),
        "kick" => Some("Kick Members (Исключать участников)"),
        "ban" | "unban" => Some("Ban Members (Блокировать участников)"),
        "clear" => Some("Manage Messages (Управлять сообщениями)"),
        "slowmode" | "lock" | "unlock" => Some("Manage Channels (Управлять каналами)"),
        "room_create" | "room_setup" => {
            Some("Manage Channels и Manage Roles (Управлять каналами и ролями)")
        }
        "room_rename" | "room_limit" | "room_delete" => {
            Some("Manage Channels (Управлять каналами)")
        }
        "room_lock" | "room_visibility" | "room_invite" | "room_transfer" => {
            Some("Manage Roles (Управлять ролями)")
        }
        "room_kick" => Some(
            "Manage Roles (Управлять ролями) и Move Members (Перемещать участников), если участника нужно отключить из комнаты",
        ),
        "shop_buy" => Some("Manage Roles (Управлять ролями)"),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::bot_requirement_for_command;

    #[test]
    fn bot_requirements_are_specific_to_the_action() {
        assert!(bot_requirement_for_command("kick")
            .unwrap()
            .contains("Kick Members"));
        assert!(bot_requirement_for_command("ban")
            .unwrap()
            .contains("Ban Members"));
        assert!(bot_requirement_for_command("clear")
            .unwrap()
            .contains("Manage Messages"));
        assert!(bot_requirement_for_command("play").is_none());
        assert!(bot_requirement_for_command("modlog").is_none());
    }
}
