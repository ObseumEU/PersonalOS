"""A clearly read-only `node -e` runs without asking (prod 2026-10-07 18:06: the Kniha Developer's look into a test
file was refused because the hook split the script at its `;`); anything that could write, spawn or reach the
network still goes to the allow-list / the CTO."""

import pytest

from pos import command_policy

WORK = "/work/kniha"
PROD_CASE = ("node -e \"const fs=require('fs');const p='/work/kniha/.tools/wt-main/app/tests/';"
             "const s=fs.readFileSync(p+'hovor-limity.test.ts','utf8');"
             "const i=s.indexOf(\\\"describe('časové limity\\\");console.log(i)\"")


def allowed(command: str) -> bool:
    return command_policy.auto_allow(command, WORK, WORK)


def test_commands_split_outside_quotes_only():
    assert command_policy.split_commands(PROD_CASE) == [PROD_CASE]
    assert command_policy.split_commands("git status && ls | head; echo 'a;b|c' || true") == [
        "git status", "ls", "head", "echo 'a;b|c'", "true"]


@pytest.mark.parametrize("command", [
    PROD_CASE,
    "node -e \"console.log(require('fs').readdirSync('.').length)\"",
    "node -p \"require('fs').readFileSync('package.json','utf8').split('\\n')[0]\"",
    "node --eval \"const path=require('path');const {readFileSync}=require('fs');"
    "console.log(readFileSync(path.join('a','b'),'utf8').length)\" | head -5",
    "node -e \"const fs=require('node:fs');process.stdout.write(String(fs.statSync('a').size))\" a b",
])
def test_read_only_node_eval_is_allowed(command):
    assert allowed(command)


@pytest.mark.parametrize("command", [
    "node -e \"require('fs').writeFileSync('x','y')\"",
    "node -e \"const fs=require('fs');fs.writeFileSync('x','y')\"",
    "node -e \"const f=require('fs');const w=f;w.unlinkSync('a')\"",
    "node -e \"const fs=require('fs');fs.promises.rm('a')\"",
    "node -e \"require('child_process').execSync('id')\"",
    "node -e \"const {writeFileSync}=require('fs');writeFileSync('a','b')\"",
    "node -e \"const {readFileSync: r, rmSync}=require('fs')\"",
    "node -e \"console.log(process.env)\"",
    "node -e \"console.log(process['env'])\"",
    "node -e \"const p=process;console.log(p.env)\"",
    "node -e \"[]['constr'+'uctor']['constr'+'uctor']('return 1')()\"",
    "node -e \"const m='x';const a=[][m]\"",
    "node -e \"const {a, [k]: F} = []\"",
    "node -e \"fetch('http://x',{method:'POST'})\"",
    "node -e \"require('https').get('https://x')\"",
    "node -e \"const r=require;r('fs')\"",
    "node -e \"require(name)\"",
    "node -e \"import('fs')\"",
    "node -e \"console.log(`${1}`)\"",
    "node -e \"\\u0065val('1')\"",
    "node -e \"console.log('$HOME')\"",
    "node -r ./evil.js -e \"1\"",
    "node -e \"1\" --require ./evil.js",
    "node script.js",
    "node -e \"console.log(1)\" > out.txt",
])
def test_anything_else_is_not(command):
    assert not allowed(command)


def test_the_hook_reads_the_cli_allow_list_outside_quotes():
    from pos_worker import command_hook

    assert command_hook.cli_allows(PROD_CASE, ["node:*"])
    assert not command_hook.cli_allows("node -e \"1\"; docker ps", ["node:*"])
    assert command_hook.split_commands("a 'x;y' && b") == ["a 'x;y'", "b"]
