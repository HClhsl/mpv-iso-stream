-- iso_bd_play.lua —— mpv 内一键播放剪贴板中的网络蓝光原盘 ISO
--
-- 用法：在 Alist/115 网页上复制 ISO 直链 → 在 mpv 中按 Ctrl+b
--       （或 script-message iso-play [链接]）
-- 脚本自动完成：选空闲端口 → 启动 iso_stream_proxy.py 解析 → 等待就绪 → 播放正片
-- 配合 iso_bd_chapters.lua：播放后自动加载章节，Ctrl+M 打开章节菜单。
-- 依赖：Python 3.8+（优先用 mpv 根目录的 python.exe）与 iso_stream_proxy.py；
--       Windows 10+ 自带 curl，剪贴板读取使用 PowerShell。
--
-- 说明：代理随 mpv 退出而结束；换新链接时重新复制后按 Ctrl+b 即可。

local mp = require "mp"
local utils = require "mp.utils"
local msg = require "mp.msg"

local BIND_KEY = "Ctrl+b"
local POLL_INTERVAL = 1      -- 秒
local POLL_TIMEOUT = 300     -- 解析超时（秒）

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

local state = { parsing = false, playing = false, port = nil, url = nil }

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
            osd("解析完成，开始播放正片", 2)
            mp.commandv("loadfile", base .. "/movie.m2ts", "replace")
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

mp.add_key_binding(BIND_KEY, "iso-play-clipboard", function()
    start(nil)
end, { repeatable = false })
