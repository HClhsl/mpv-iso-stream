-- iso_bd_chapters.lua —— 网络蓝光 ISO 的章节菜单（配合 iso_stream_proxy.py v2）
--
-- 用 iso_stream_proxy.py 播放 Alist/115 上的蓝光原盘时，代理把主播放列表
-- 拼成 /movie.m2ts（或 /bd/<编号>.m2ts），并从 PlayMark 提取章节放在
-- 同路径的 .json 里。本脚本在检测到此类地址时拉取章节元数据，
-- 按 Ctrl+M 用 uosc 弹出章节菜单，选中即精确跳转；uosc 不存在时退化为 OSD 列表。
--
-- 依赖：代理运行中；Windows 10+ 自带的 curl.exe。

local mp = require "mp"
local utils = require "mp.utils"
local msg = require "mp.msg"

local SCRIPT_NAME = mp.get_script_name()
local MENU_ID = "bdmv-chapters"
local BIND_KEY = "Ctrl+m"

local meta = nil      -- {name=, duration=, chapters={{t=,title=},...}}
local meta_url = nil  -- 章节元数据地址（对应正在播放的列表）
local fetching = false

local function parse_proxy_url(path)
    -- 去掉防断点续播用的查询参数
    path = path:gsub("%?.*$", "")
    -- 支持 /bd/<编号>/<集>.m2ts、/bd/<编号>.m2ts 与 /movie.m2ts
    local host, name, idx = path:match(
        "^http://127%.0%.0%.1:(%d+)/bd/([^/]+)/(%d+)%.m2ts$")
    if host then
        local base = "http://127.0.0.1:" .. host
        return base, base .. "/bd/" .. name .. "/" .. idx .. ".json"
    end
    host, name = path:match("^http://127%.0%.0%.1:(%d+)/bd/([^/]+)%.m2ts$")
    if host then
        local base = "http://127.0.0.1:" .. host
        return base, base .. "/bd/" .. name .. ".json"
    end
    host = path:match("^http://127%.0%.0%.1:(%d+)/movie%.m2ts$")
    if host then
        local base = "http://127.0.0.1:" .. host
        return base, base .. "/movie.json"
    end
    return nil
end

local function fetch_meta(url, cb)
    mp.command_native_async({
        name = "subprocess",
        args = { "curl", "-s", "--max-time", "8", url },
        capture_stdout = true,
        playback_only = false,
    }, function(ok, res)
        if not ok or not res or res.status ~= 0 or not res.stdout or res.stdout == "" then
            cb(nil, res and res.status or -1)
            return
        end
        local data = utils.parse_json(res.stdout)
        cb(data)
    end)
end

local function seek_to(t)
    mp.commandv("seek", tostring(t), "absolute+exact")
end

local function osd_list()
    if not meta or #meta.chapters == 0 then
        mp.osd_message("此播放列表没有章节标记", 2)
        return
    end
    local lines = { "章节（" .. #meta.chapters .. "，用 uosc 菜单更直观）：" }
    for i, ch in ipairs(meta.chapters) do
        local s = math.floor(ch.t)
        lines[#lines + 1] = string.format("%2d  %02d:%02d:%02d  %s",
            i, math.floor(s / 3600), math.floor(s / 60) % 60, s % 60, ch.title)
    end
    mp.osd_message(table.concat(lines, "\n"), 8)
end

local function open_menu()
    if not meta or #meta.chapters == 0 then
        osd_list()
        return
    end
    local items = {}
    for _, ch in ipairs(meta.chapters) do
        local s = math.floor(ch.t)
        items[#items + 1] = {
            title = string.format("%02d:%02d:%02d",
                math.floor(s / 3600), math.floor(s / 60) % 60, s % 60),
            hint = ch.title,
            value = ch.t,
            keep_open = false,
        }
    end
    mp.commandv("script-message-to", "uosc", "open-menu", utils.format_json({
        type = MENU_ID,
        title = "章节 — " .. (meta.name or ""),
        items = items,
        callback = { SCRIPT_NAME, "bdmv-menu-event" },
    }))
end

local function load_meta(force)
    if not meta_url or (meta and not force) or fetching then
        return
    end
    fetching = true
    fetch_meta(meta_url, function(data, status)
        fetching = false
        if not data or type(data.chapters) ~= "table" then
            msg.warn("章节元数据获取失败（status=" .. tostring(status) .. "）")
            return
        end
        meta = data
        msg.info(string.format("已加载章节：%d 个（%s）", #data.chapters, data.name or "?"))
        if #data.chapters > 0 then
            mp.osd_message(string.format("BDMV：%s，%d 章（Ctrl+M 打开章节菜单）",
                data.name or "", #data.chapters), 3)
        else
            mp.osd_message(string.format("BDMV：%s（无章节标记）", data.name or ""), 2)
        end
    end)
end

mp.register_script_message("bdmv-menu-event", function(json)
    local ev = utils.parse_json(json)
    if ev and ev.type == "activate" and ev.value ~= nil then
        seek_to(tonumber(ev.value) or 0)
        mp.commandv("script-message-to", "uosc", "close-menu", MENU_ID)
    end
end)

mp.add_key_binding(BIND_KEY, "bdmv-chapters-menu", function()
    if not meta_url then
        mp.osd_message("当前不是 iso-stream-proxy 的播放流", 2)
        return
    end
    if meta then
        open_menu()
    else
        load_meta(false)
        osd_list()
    end
end, { repeatable = false })

mp.register_event("file-loaded", function()
    meta = nil
    local path = mp.get_property("path") or ""
    local _, murl = parse_proxy_url(path)
    meta_url = murl
    if meta_url then
        msg.info("检测到代理流：" .. path)
        load_meta(false)
    end
end)
