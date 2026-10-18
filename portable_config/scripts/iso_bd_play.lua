-- iso_bd_play.lua —— mpv 内一键播放剪贴板中的网络蓝光原盘 ISO
--
-- 用法：在 Alist/115 网页上复制 ISO 直链 → 在 mpv 中按 Ctrl+b
--       （或 script-message iso-play [链接]）
-- 脚本自动完成：选空闲端口 → 启动 iso_stream_proxy.py 解析 → 等待就绪 → 播放正片
-- 多集盘会自动弹出剧集选择菜单；Ctrl+L 可随时再次打开。
-- 配合 iso_bd_chapters.lua：播放后自动加载章节，Ctrl+M 打开章节菜单。
-- 依赖：Python 3.8+（优先用 mpv 根目录的 python.exe）与 iso_stream_proxy.py；
--       Windows 10+ 自带 curl，剪贴板读取使用 PowerShell。
--
-- 说明：代理随 mpv 退出而结束；换新链接时重新复制后按 Ctrl+b 即可。

local mp = require "mp"
local utils = require "mp.utils"
local msg = require "mp.msg"

local SCRIPT_NAME = mp.get_script_name()
local BIND_KEY = "Ctrl+b"
local TITLE_KEY = "Ctrl+l"   -- 重新打开剧集/分段选择
local POLL_INTERVAL = 1      -- 秒
local POLL_TIMEOUT = 300     -- 解析超时（秒）
-- 每次播放使用唯一 URL 参数：避免与 mpv 的 watch-later 断点续播撞车
-- （代理地址固定为 /movie.m2ts，不加参数会沿用上一部片的进度）
local SESSION = string.format("%d_%d", os.time(),
                              math.floor((os.clock() * 1000) % 1000000))
local HAS_UOSC = utils.file_info(utils.join_path(
    mp.get_property("config-dir") or "", "scripts", "uosc", "main.lua")) ~= nil

-- mpv-lazy 根目录 = portable_config 的上级；独立安装时退回脚本目录
local cfg = mp.get_property("config-dir") or ""
local root = cfg:match("^(.*)[/\\]portable_config[/\\]?$") or cfg
local script_dir = (mp.get_script_directory and mp.get_script_directory()) or ""

local function file_exists(path)
    return path and path ~= "" and utils.file_info(path) ~= nil
end

-- 代理脚本：优先 mpv 根目录（mpv-lazy 惯例），否则脚本自身目录
local PROXY = utils.join_path(root, "iso_stream_proxy.py")
if not file_exists(PROXY) then
    local alt = utils.join_path(script_dir, "iso_stream_proxy.py")
    if file_exists(alt) then
        PROXY = alt
    end
end

-- Python：优先 mpv-lazy 内嵌 python.exe，其次 PATH 中的 python
local PYTHON = "python"
local bundled = utils.join_path(root, "python.exe")
if file_exists(bundled) then
    PYTHON = bundled
end

local LOG = utils.join_path(root ~= "" and root or script_dir, "iso-play-last.log")
local PORTS = { 8080, 8081, 8082, 8083, 8084 }

local state = { parsing = false, playing = false, port = nil, url = nil, catalog = nil }

local function osd(text, dur)
    mp.osd_message(text, dur or 2)
    msg.info(text)
end

local function curl_code(url, cb)
    mp.command_native_async({
        name = "subprocess",
        args = { "curl", "-s", "--head", "--max-time", "3", "-o", "NUL",
                 "-w", "%{http_code}", url },
        capture_stdout = true,
        playback_only = false,
    }, function(ok, res)
        local code = (ok and res and res.status == 0 and res.stdout or ""):match("%d%d%d") or "000"
        cb(code)
    end)
end

local function get_clipboard(cb)
    mp.command_native_async({
        name = "subprocess",
        args = { "powershell", "-NoProfile", "-Command", "Get-Clipboard" },
        capture_stdout = true,
        playback_only = false,
    }, function(ok, res)
        local text = (ok and res and res.status == 0 and res.stdout or "")
        text = text:match("[^\r\n]+") or ""
        text = text:match("^%s*(.-)%s*$")
        cb(text)
    end)
end

local function curl_json(url, cb)
    mp.command_native_async({
        name = "subprocess",
        args = { "curl", "-s", "--max-time", "8", url },
        capture_stdout = true,
        playback_only = false,
    }, function(ok, res)
        if not ok or not res or res.status ~= 0 or not res.stdout then
            cb(nil)
            return
        end
        cb(utils.parse_json(res.stdout))
    end)
end

local function session_url(path)
    return "http://127.0.0.1:" .. state.port .. path .. "?s=" .. SESSION
end

local function find_playlist(cat)
    if not cat then return nil end
    for _, p in ipairs(cat.playlists or {}) do
        if p.name == cat.main then return p end
    end
    return nil
end

local function episode_items(p)
    local out = {}
    for _, it in ipairs(p and p.items or {}) do
        if it.duration >= 600 then
            out[#out + 1] = it
        end
    end
    return out
end

-- 按内容签名去重（同内容不同音轨/版本只留一个），供手动选单使用
local function candidate_playlists(cat, min_dur)
    local out, seen = {}, {}
    for _, p in ipairs(cat and cat.playlists or {}) do
        if not p.looped and (p.duration or 0) >= min_dur then
            local parts = {}
            for _, it in ipairs(p.items or {}) do
                parts[#parts + 1] = string.format("%s:%.0f",
                    tostring(it.clip), it.duration or 0)
            end
            local sig = table.concat(parts, ",")
            if not seen[sig] then
                seen[sig] = true
                out[#out + 1] = p
            end
        end
    end
    return out
end

-- manual=true：Ctrl+L 手动打开，列出所有非循环播放列表
local function show_titles(cat, manual)
    local p = find_playlist(cat)
    if not p then return false end
    local eps = episode_items(p)
    local by_item = #eps >= 2
    local cands = nil
    if not by_item then
        if not manual then return false end
        cands = candidate_playlists(cat, 60)
        if #cands < 2 then return false end
    end

    if not HAS_UOSC then
        if by_item then
            mp.commandv("loadfile",
                session_url("/bd/" .. p.name .. "/" .. eps[1].index .. ".m2ts"), "replace")
            mp.osd_message("未检测到 uosc，已播放第 1 段", 3)
        else
            mp.osd_message("剧集菜单需要 uosc", 2)
        end
        return true
    end

    local items = {}
    if by_item then
        for _, it in ipairs(eps) do
            local d = math.floor(it.duration)
            items[#items + 1] = {
                title = string.format("第 %d 段  %02d:%02d", it.index + 1,
                                      math.floor(d / 60), d % 60),
                value = "item:" .. it.index,
                keep_open = false,
            }
        end
        items[#items + 1] = { title = "连续播放全部", value = "all" }
    else
        for _, cp in ipairs(cands) do
            local d = math.floor(cp.duration)
            items[#items + 1] = {
                title = string.format("%s  %02d:%02d:%02d", cp.name,
                    math.floor(d / 3600), math.floor(d / 60) % 60, d % 60),
                value = "pl:" .. cp.name,
                keep_open = false,
            }
        end
    end
    mp.commandv("script-message-to", "uosc", "open-menu", utils.format_json({
        type = "iso-titles",
        title = "选择剧集/播放列表" .. (by_item and (" — " .. p.name) or ""),
        items = items,
        callback = { SCRIPT_NAME, "iso-title-event" },
    }))
    mp.osd_message("请选择要播放的剧集（Ctrl+L 可再次打开）", 3)
    return true
end

local function play_chosen(value)
    local plname = (value or ""):match("^pl:(.+)$")
    if plname then
        mp.commandv("loadfile", session_url("/bd/" .. plname .. ".m2ts"), "replace")
        return
    end
    local p = find_playlist(state.catalog)
    if not p then return end
    if value == "all" then
        mp.commandv("loadfile", session_url("/movie.m2ts"), "replace")
        return
    end
    local idx = tonumber((value or ""):match("^item:(%d+)$"))
    if idx then
        mp.commandv("loadfile",
            session_url("/bd/" .. p.name .. "/" .. idx .. ".m2ts"), "replace")
    end
end

mp.register_script_message("iso-title-event", function(json)
    local ev = utils.parse_json(json)
    if ev and ev.type == "activate" and ev.value ~= nil then
        play_chosen(tostring(ev.value))
        mp.commandv("script-message-to", "uosc", "close-menu", "iso-titles")
    end
end)

local function find_free_port(i, cb)
    if i > #PORTS then
        cb(nil)
        return
    end
    local p = PORTS[i]
    curl_code("http://127.0.0.1:" .. p .. "/", function(code)
        if code == "000" then  -- 连接失败 = 端口空闲
            cb(p)
        else
            find_free_port(i + 1, cb)
        end
    end)
end

local function poll_ready(t0)
    local base = "http://127.0.0.1:" .. state.port
    curl_code(base .. "/", function(code)
        if not state.parsing then
            return  -- 已被取消/超时
        end
        local el = os.time() - t0
        if code == "200" then
            state.parsing = false
            state.playing = true
            osd("解析完成", 2)
            curl_json(base .. "/catalog.json", function(cat)
                state.catalog = cat
                if not show_titles(cat) then
                    mp.commandv("loadfile", session_url("/movie.m2ts"), "replace")
                end
            end)
        elseif code == "503" then
            -- 代理已启动、仍在解析 BDMV
            mp.osd_message(string.format("正在解析原盘导航数据… %d 秒", el), 1)
            mp.add_timeout(POLL_INTERVAL, function() poll_ready(t0) end)
        elseif code == "000" then
            -- 连接失败 = 代理进程已退出（失败原因由 subprocess 回调提示）
            state.parsing = false
        elseif el >= POLL_TIMEOUT then
            state.parsing = false
            osd("解析超时，请查看 " .. LOG, 4)
        else
            mp.add_timeout(POLL_INTERVAL, function() poll_ready(t0) end)
        end
    end)
end

local function start(url)
    if state.parsing then
        osd("正在解析中，请稍候…", 2)
        return
    end
    local function go(u)
        u = (u or ""):match("[^\r\n]+") or ""
        u = u:match("^%s*(.-)%s*$") or ""
        if u:match("^https?://") == nil then
            osd("剪贴板里没有可用的 http 链接（先在网盘页面复制 ISO 直链）", 3)
            return
        end
        if not file_exists(PROXY) then
            osd("未找到 iso_stream_proxy.py（应放在 mpv 根目录或脚本目录）", 4)
            return
        end
        find_free_port(1, function(port)
            if not port then
                osd("8080-8084 端口都被占用", 3)
                return
            end
            state.parsing = true
            state.playing = false
            state.port = port
            state.url = u
            osd("启动 iso-stream-proxy（端口 " .. port .. "）…", 2)
            msg.info("启动代理：" .. u)
            mp.command_native_async({
                name = "subprocess",
                args = { PYTHON, PROXY, u, "--port", tostring(port),
                         "--log-file", LOG },
                capture_stdout = true,
                capture_stderr = true,
                playback_only = false,
            }, function(ok, res)
                -- 代理退出（正常退出 / 401 / 解析失败 / mpv 退出连带）
                local out = res and ((res.stdout or "") .. (res.stderr or "")) or ""
                pcall(function()
                    local f = io.open(LOG, "wb")
                    if f then
                        f:write(out)
                        f:close()
                    end
                end)
                if state.parsing then
                    state.parsing = false
                    osd("代理已退出，详见 " .. LOG, 4)
                else
                    msg.info("代理已退出")
                end
            end)
            poll_ready(os.time())
        end)
    end
    if url and #url > 0 then
        go(url)
    else
        get_clipboard(go)
    end
end

mp.register_script_message("iso-play", function(url)
    start(url)
end)

local function open_titles()
    if not state.port or not state.catalog then
        osd("当前没有可用的剧集列表（先按 Ctrl+b 播放原盘）", 3)
        return
    end
    if not HAS_UOSC then
        mp.osd_message("剧集菜单需要 uosc", 2)
        return
    end
    if not show_titles(state.catalog, true) then
        mp.osd_message("没有可选择的剧集/播放列表", 2)
    end
end

mp.register_script_message("iso-titles", open_titles)
mp.add_key_binding(TITLE_KEY, "iso-titles-menu", open_titles, { repeatable = false })

mp.add_key_binding(BIND_KEY, "iso-play-clipboard", function()
    start(nil)
end, { repeatable = false })
