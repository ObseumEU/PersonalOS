/** The whole Czech dictionary (every area), for the full app: see ./core.ts. Keys are unique across areas. */
import agents from "./cs/agents";
import chat from "./cs/chat";
import common from "./cs/common";
import home from "./cs/home";
import knowledge from "./cs/knowledge";
import mobile from "./cs/mobile";
import projects from "./cs/projects";
import report from "./cs/report";
import settings from "./cs/settings";
import team from "./cs/team";
import tickets from "./cs/tickets";
import work from "./cs/work";

import { register } from "./core";

register(common, home, chat, agents, work, knowledge, settings, team, tickets, projects, mobile, report);

export * from "./core";
