// The installed app's first screens need only these areas of the dictionary (i18n/core.ts);
// the rest (Nastavení, Tým, Znalosti…) registers when the task panel or settings load, or when idle.
import chat from "../i18n/cs/chat";
import common from "../i18n/cs/common";
import home from "../i18n/cs/home";
import mobile from "../i18n/cs/mobile";
import team from "../i18n/cs/team";
import work from "../i18n/cs/work";
import { register } from "../i18n/core";

register(common, home, chat, work, team, mobile);
