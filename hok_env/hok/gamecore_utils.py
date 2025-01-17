import datetime
import json
import math
import os
import re
import shutil
import traceback
import time
import signal
import struct
import subprocess
import requests
import random
import urllib3
from urllib3.exceptions import InsecureRequestWarning

urllib3.disable_warnings(InsecureRequestWarning)


def get_angle(p):
    x, z = p[0], p[1]
    if x == 0:
        deg = -90 if z > 0 else 90
    else:
        deg = math.atan2(-z, x) * (180 / math.pi)
    return deg


class GameProc:
    def __init__(self, command, close_command, timeout=1, auto_kill=True):
        self.proc = subprocess.Popen([command], preexec_fn=os.setpgrp, shell=True)
        self.timeout = timeout
        self.close_command = close_command
        self.auto_kill = auto_kill

    def __call__(self, *args, **kwargs):
        return self.proc

    def __del__(self):
        if not self.auto_kill:
            return

        try:
            self.proc.wait(self.timeout)

        except subprocess.TimeoutExpired:
            print("GameCore Process TIMEOUT {}, kill it.".format(self.timeout))
            os.killpg(self.proc.pid, signal.SIGKILL)
        os.system(self.close_command)


class GameLauncher:
    def __del__(self):
        # self.close_game(keep_zmq=False)
        pass
    def clear_runtime_path(self):
        runtime_path = self.runtime_path
        if not os.path.exists(runtime_path):
            os.system("mkdir -p {}".format(runtime_path))
            # os.system("cd {}; cp ../../hero_*.conf ./; ln -s ../../core_assets core_assets".format(runtime_path))
        else:
            # clean useless path
            os.system(
                "cd {}; rm core.* *.abs *stat *.log core_assets core > /dev/null 2>&1".format(
                    runtime_path
                )
            )
        os.system(
            "cd {}; ln -s {} core; cp core/hero_*.conf ./; ln -s core/core_assets core_assets".format(
                runtime_path, self.gamecore_path
            )
        )

    def __init__(
        self, runtime_id, log_path=None, gamecore_path=None, lib_processor=None
    ):
        if log_path is None:
            log_path = "./log"
        if gamecore_path is None:
            gamecore_path = "~/.hok"
        # running path / log path
        self.num_player = 2
        self.runtime_id = runtime_id
        log_path = os.path.abspath(os.path.expanduser(log_path))
        self.log_path = log_path

        # check log path
        if not os.path.exists(log_path):
            try:
                os.mkdir(log_path)
            except FileExistsError:
                print("[warning] log path {} exists".format(log_path))

        # gamecore path
        gamecore_path = os.path.expanduser(gamecore_path)
        self.gamecore_path = gamecore_path
        self.runtime_path = os.path.join(
            self.gamecore_path, "runtime/{}".format(self.runtime_id)
        )
        self.gc_proc = None
        self.proc_timeout = 1
        self.config_modifier = ConfigModifier(
            gamecore_path=gamecore_path,
            runtime_id=runtime_id,
            runtime_path=self.runtime_path,
        )
        # zmq
        self.addrs = [None] * self.num_player
        self.server = [None] * self.num_player

        self.need_close = False
        self.lib_processor = lib_processor
        self.recv_buffer_size = 1 << 20

    def generate_game_id(self):
        dt = datetime.datetime.now()
        game_id = (
            "gameid-" + dt.strftime("%Y%m%d-%H%M%S") + "-{}".format(self.runtime_id)
        )
        return game_id

    def _launch_proc(self, auto_kill=True):
        # start a new game
        game_id = self.config_modifier.common_config.get("game_id")
        if game_id is None:
            game_id = self.generate_game_id()

        print("runtime_path: ", self.runtime_path)

        command = (
            "cd {};".format(self.runtime_path)
            + 'LD_LIBRARY_PATH="./:./core_assets:${{LD_LIBRARY_PATH}}" '
            "nohup stdbuf -oL core/sgame_simulator_remote_zmq '{}' "
            "'./sgame_simulator.conf' >> ./{}.log 2>&1".format(game_id, game_id)
        )
        print("command:", command)

        if auto_kill:
            close_command = "cd {runtime_path}; mv AIOSS*.abs *stat *log {game_log_path}/ > /dev/null 2>&1".format(
                runtime_path=self.runtime_path, game_log_path=self.log_path
            )
        else:
            close_command = ""

        self.gc_proc = GameProc(
            command, close_command, timeout=self.proc_timeout, auto_kill=auto_kill
        )

    def _launch_zmq_server(self):
        common_dict = self.config_modifier.common_config

        # connect zmq
        for i in range(self.num_player):
            ip = common_dict["ip"]
            addr = "tcp://{}:{}".format(
                "0.0.0.0", int(self.config_modifier.hero_info[i]["port"])
            )
            if self.addrs[i] != addr:
                if self.server[i] is not None:
                    self.server[i].Close()
                    self.lib_processor.server_manager.Delete(self.addrs[i])
                    self.server[i] = None

                num_retry = 5
                for j in range(num_retry):
                    try:
                        zmq_server = self.lib_processor.server_manager.Add(addr)
                        if not zmq_server:
                            raise Exception("Address already exists: {}".format(addr))
                        rc = zmq_server.Reset(addr)
                        if rc < 0:
                            raise Exception("zmq_server.Reset failed")
                        self.server[i] = zmq_server
                        self.addrs[i] = addr
                        break
                    except socket.error:
                        print(
                            "[error] socket bind error, wait 1 sec and try {}/{}".format(
                                j + 1, num_retry
                            )
                        )
                        traceback.print_exc()
                        time.sleep(1)

    def start(self, config_dict, common_config):
        assert os.path.exists(
            self.gamecore_path
        ), "ERROR: gamecore path {} not exists!".format(self.gamecore_path)

        # create path
        self.clear_runtime_path()
        camp2_hero_name = config_dict[1]["hero"]
        common_config = common_config.copy()
        common_config["init_abs"] = os.path.join(
            self.gamecore_path, "init_abs/{}.abs".format(camp2_hero_name)
        )

        self.config_modifier.dump_config(config_dict, common_config)
        self.need_close = True
        self._launch_proc()
        self._launch_zmq_server()

    def _close_proc(self):
        if hasattr(self, "gc_proc") and self.gc_proc is not None:
            del self.gc_proc
            self.gc_proc = None

    def _close_zmq_server(self):
        for i in range(2):
            if self.server[i] is not None:
                self.server[i].Close()
                self.lib_processor.server_manager.Delete(self.addrs[i])
                self.server[i] = None

    def close_game(self, keep_zmq=True,game_id=None):
        self._close_proc()
        if not keep_zmq:
            self._close_zmq_server()

    def send_msg(self, msg, id):
        total_len = 4 + len(msg)
        header = struct.pack("I", total_len)
        msg = header + msg
        self.conns[id].send(msg)

    def recv_msg(self, id):
        header = self.conns[id].recv()
        length = struct.unpack(">I", header[:4])[0]
        req_type = struct.unpack("I", header[4:8])[0]
        seq_no = struct.unpack("I", header[8:12])[0]
        length -= 12
        obs = header[12:]

        return length, req_type, seq_no, obs


def send_http_request(
    server_addr, req_type, token, config, download_path=None, no_python=False
):
    if server_addr is None:
        server_addr = "127.0.0.1:23333"
    url = "http://{}".format(server_addr)
    headers = {
        "Content-Type": "application/json",
    }
    data = config

    resp = requests.post(
        url=url, data=json.dumps(data), headers=headers, verify=False
    )
    if resp.ok:
        ret = resp.json()
        return ret
    else:
        return {}


class RemoteGameProc:
    def __init__(
        self,
        user_token,
        runtime_id,
        launch_server,
        start_config,
        log_path,
        close_command,
        need_download=True,
        game_id=""
    ):
        self.user_token = user_token
        self.runtime_id = runtime_id
        self.launch_server = launch_server
        self.start_config = start_config.copy()
        self.start_config["runtime_id"] = self.runtime_id
        self.need_download = need_download
        self.close_command = close_command
        self.log_path = log_path
        self.game_id=game_id
        self._remote_start()


    def _remote_start(self):
        sgame_info={}
        config_dict=self.start_config["config_dict"]
        common_config=self.start_config["common_config"]
        camp2_hero_name = config_dict[1]["hero"]
        sgame_info["abs_name"]=os.path.join("{}.abs".format(camp2_hero_name))
        hero_conf=[]
        for i in range(len(config_dict)):
            hero_dict={}
            hero_dict["hero_id"]=ConfigModifier.HERO_DICT[config_dict[i]["hero"]]
            if not config_dict[i]["use_common_ai"]:
                hero_dict["request_info"]={"ip":common_config["ip"],"port":config_dict[i]["port"],"timeout":20000}
            hero_dict["summon_id"]=ConfigModifier.SKILL_DICT[config_dict[i]["skill"]]
            hero_dict["symbol"]=ConfigModifier.SYMBOL[ConfigModifier.HERO_DICT[config_dict[i]["hero"]]]
            hero_conf.append(hero_dict)
        sgame_info["hero_conf"]=hero_conf
        # sgame_info["gameover"] = False
        sgame_info["game_id"]=self.game_id

        send_http_request(
            self.launch_server, "newGame", self.user_token, sgame_info
        )

    def _download_results(self, no_python=False):
        if not self.need_download:
            return
        ret = send_http_request(
            self.launch_server,
            "list",
            self.user_token,
            {"runtime_id": self.runtime_id},
            no_python=no_python,
        )

        for f in ret["data"]["files"]:
            print("download file:", f)
            ret = send_http_request(
                self.launch_server,
                "download",
                self.user_token,
                {"runtime_id": self.runtime_id},
                download_path=f,
                no_python=no_python,
            )
            local_fname = os.path.join(self.log_path, f)
            with open(local_fname, "wb") as f:
                shutil.copyfileobj(ret.raw, f)

        self.need_download = False

    def __del__(self):
        print("check game stopped.")

class GameLauncherRemote(GameLauncher):
    def __init__(
        self,
        runtime_id,
        log_path=None,
        gamecore_path=None,
        launch_server=None,
        local_server=True,
        aiserver_ip=None,
        lib_processor=None,
    ):
        # use some common function only, do not call super.__init__
        super(GameLauncherRemote, self).__init__(runtime_id, log_path, gamecore_path)
        if aiserver_ip is None:
            aiserver_ip = "127.0.0.1"
        self.aiserver_ip = aiserver_ip

        self.launch_server = launch_server
        self.local_server = local_server
        self.remote_gc_proc = None
        self.load_token()
        self.lib_processor = lib_processor
        self.gameover_sent = True
        self.config_modifier.common_config["ip"]=self.aiserver_ip

    def load_token(self, path="~/.hok/token"):
        # path = os.path.expanduser(path)
        # assert os.path.exists(path), "[ERROR] token file {} not exists!".format(path)
        # with open(path, "r") as f:
        #     self.user_token = f.read().strip()
        self.user_token = self.aiserver_ip.replace(".", "D")

    def _launch_remote_proc(self, start_config):
        if self.local_server:
            close_command = "cd {runtime_path}; mv AIOSS*.abs *stat *log {game_log_path}/ > /dev/null 2>&1".format(
                runtime_path=self.runtime_path, game_log_path=self.log_path
            )
        else:
            close_command = ""
        self.remote_gc_proc = RemoteGameProc(
            self.user_token,
            self.runtime_id,
            self.launch_server,
            start_config,
            log_path=self.log_path,
            close_command=close_command,
            need_download=not self.local_server,
            game_id=start_config["common_config"]["game_id"]
        )

    def _close_remote_proc(self):
        if hasattr(self, "remote_gc_proc") and self.remote_gc_proc is not None:
            # del self.remote_gc_proc
            self.remote_gc_proc = None

    def start(self, config_dict, common_config, need_log=False):
        # "curl -k https://127.0.0.1:23333/v1/newGame -d '{"Token": "123", "CustomConfig": "xxxxx"}'"
        assert os.path.exists(
            self.gamecore_path
        ), "ERROR: gamecore path {} not exists!".format(self.gamecore_path)

        for d in config_dict:
            if d is not None and d.get("ip") is not None:
                d["ip"] = self.aiserver_ip
        common_config["ip"] = self.aiserver_ip
        self.gameover_sent=False
        self.start_config = {"config_dict": config_dict, "common_config": common_config}
        self.config_modifier.update_config(config_dict, common_config)
        self.need_close = True
        self._launch_zmq_server()
        self._launch_remote_proc(self.start_config)

    def close_game(self, keep_zmq=True,game_id=None):
        # assert game_id is not None
        # self.gameover_sent=True
        # sgame_info = {}
        # sgame_info["abs_name"] = "./1V1.abs"
        # config_dict = self.start_config["config_dict"]
        # common_config = self.start_config["common_config"]
        # hero_conf = []
        # for i in range(len(config_dict)):
        #     hero_dict = {}
        #     hero_dict["hero_id"] = ConfigModifier.HERO_DICT[config_dict[i]["hero"]]
        #     if not config_dict[i]["use_common_ai"]:
        #         hero_dict["request_info"] = {"ip": common_config["ip"], "port": config_dict[i]["port"],
        #                                      "timeout": 20000}
        #     hero_dict["summon_id"] = ConfigModifier.SKILL_DICT[config_dict[i]["skill"]]
        #     hero_dict["symbol"] = ConfigModifier.SYMBOL[ConfigModifier.HERO_DICT[config_dict[i]["hero"]]]
        #     hero_conf.append(hero_dict)
        # sgame_info["hero_conf"] = hero_conf
        # sgame_info["gameover"] = True
        # sgame_info["game_id"]=game_id
        # send_http_request(
        #     self.launch_server,
        #     "download",
        #     self.user_token,
        #     sgame_info,
        #     download_path=None,
        #     no_python=True)
        self._close_remote_proc()
        if not keep_zmq:
            self._close_zmq_server()


# heal: 80102, sprint: 80109, punish: 80104|80116,
# execute: 80108, rage: 80110, disrupt: 80105, daze: 80103
# purity: 80107, weak: 80121, flash: 80115


class ConfigModifier:
    HERO_DICT = {
        "diaochan": 141,
        "luban": 112,
        "luna": 146,
        "lvbu": 123,
        "jvyoujing": 163,
        "miyue": 121,
        "libai": 131,
        "makeboluo": 132,
        "direnjie": 133,
        "guanyu": 140,
        "hanxin": 150,
        "huamulan": 154,
        "buzhihuowu": 157,
        "houyi": 169,
        "zhongkui": 175,
        "ganjiangmoye": 182,
        "kai": 193,
        "gongsunli": 199,
        "peiqinhu": 502,
        "shangguanwaner": 513,
    }
    SKILL_DICT = {
        "heal": 80102,
        "rage": 80110,
        "flash": 80115,
        "sprint": 80109,
        "execute": 80108,
        "disrupt": 80105,
        "daze": 80103,
        "purity": 80107,
        "weak": 80121,
    }
    SYMBOL = {
        141: "1514;1514;1514;1514;1514;1514;1514;1514;1514;1514;"
        "3516;3516;3516;3516;3516;3516;3516;3516;3516;3516;"
        "2520;2520;2520;2520;2520;2520;2520;2520;2520;2520",
        112: "1519;1519;1519;1519;1519;1519;1519;1519;1519;1519;"
        "3514;3514;3514;3514;3514;3514;3514;3514;3514;3514;"
        "2504;2504;2504;2504;2504;2504;2504;2504;2504;2504",
        146: "1520;1520;1520;1520;1520;1520;1520;1520;1520;1520;"
        "3515;3515;3515;3515;3515;3515;3515;3515;3515;3515;"
        "2520;2520;2520;2520;2520;2520;2520;2520;2520;2520",
        123: "1512;1512;1512;1512;1512;1512;1512;1512;1512;1512;"
        "3509;3509;3509;3509;3509;3509;3509;3509;3509;3509;"
        "2520;2520;2520;2520;2520;2520;2520;2520;2520;2520",
        193: "1510;1510;1510;1519;1519;1519;1519;1519;1519;1519;"
        "3514;3514;3514;3514;3514;3514;3514;3514;3514;3514;"
        "2520;2520;2520;2520;2520;2520;2515;2515;2506;2506",
        163: "1504;1504;1504;1504;1504;1504;1504;1504;1504;1520;"
        "3514;3514;3514;3514;3514;3514;3514;3514;3514;3514;"
        "2517;2517;2517;2517;2517;2517;2517;2517;2520;2520",
        121: "1519;1519;1519;1520;1520;1520;1520;1520;1520;1520;"
        "3515;3515;3515;3515;3515;3515;3515;3515;3515;3515;"
        "2520;2520;2520;2520;2520;2520;2520;2520;2520;2503",
        131: "1504;1504;1504;1504;1504;1504;1504;1520;1520;1520;"
        "3514;3514;3514;3514;3514;3514;3514;3514;3514;3514;"
        "2520;2520;2520;2520;2520;2520;2520;2504;2504;2504",
        132: "1520;1520;1520;1520;1520;1520;1520;1520;1520;1520;"
        "3514;3514;3514;3514;3514;3514;3514;3514;3514;3514;"
        "2504;2504;2504;2504;2504;2504;2504;2520;2520;2520",
        133: "1519;1519;1519;1519;1519;1519;1519;1519;1519;1519;"
        "3514;3514;3514;3514;3514;3514;3514;3514;3514;3514;"
        "2504;2504;2504;2504;2520;2520;2520;2520;2520;2520",
        140: "1504;1504;1504;1504;1504;1504;1504;1504;1504;1504;"
        "3514;3514;3514;3514;3514;3514;3514;3514;3514;3514;"
        "2517;2517;2517;2517;2517;2517;2517;2517;2517;2517",
        150: "1519;1519;1519;1519;1519;1519;1519;1519;1519;1520;"
        "3514;3514;3514;3514;3514;3514;3514;3514;3514;3514;"
        "2504;2504;2504;2504;2504;2504;2504;2504;2504;2504",
        154: "1504;1504;1504;1504;1504;1504;1504;1504;1504;1520;"
        "3514;3514;3514;3514;3514;3514;3514;3514;3514;3514;"
        "2517;2517;2515;2515;2515;2515;2515;2515;2515;2515",
        157: "1514;1514;1514;1514;1514;1514;1514;1514;1514;1514;"
        "3515;3515;3515;3515;3515;3515;3515;3515;3515;3515;"
        "2520;2520;2520;2520;2520;2520;2520;2503;2503;2503",
        169: "1510;1510;1510;1520;1520;1520;1520;1520;1520;1520;"
        "3514;3514;3514;3514;3514;3514;3514;3514;3514;3514;"
        "2520;2520;2520;2520;2520;2504;2504;2504;2504;2504",
        175: "1514;1514;1514;1514;1514;1514;1514;1514;1514;1514;"
        "3515;3515;3515;3515;3515;3515;3515;3515;3515;3515;"
        "2512;2512;2512;2512;2512;2512;2512;2512;2512;2512",
        182: "1514;1514;1514;1514;1514;1514;1514;1514;1514;1514;"
        "3515;3515;3515;3515;3515;3515;3515;3515;3515;3515;"
        "2520;2520;2520;2520;2520;2515;2515;2515;2515;2515",
        199: "1510;1510;1510;1519;1519;1519;1519;1519;1519;1520;"
        "3514;3514;3514;3514;3514;3514;3514;3514;3514;3514;"
        "2520;2520;2520;2520;2520;2504;2504;2504;2504;2504",
        513: "1514,1514,1514,1514,1514,1514,1514,1514,1514,1514,"
        "3515,3515,3515,3515,3515,3515,3515,3515,3515,3515,"
        "2520,2520,2520,2520,2520,2520,2520,2520,2520,2520",
        518: "1504,1504,1504,1504,1504,1504,1504,1504,1504,1504,"
        "3514,3514,3514,3514,3514,3514,3514,3514,3514,3514,"
        "2520,2520,2520,2520,2520,2520,2520,2520,2520,2520",
        502: "1504,1504,1504,1504,1504,1504,1504,1504,1504,1504,"
        "3514,3514,3514,3514,3514,3514,3514,3514,3514,3514,"
        "2517,2517,2517,2517,2517,2517,2517,2517,2517,2517",
        510: "1504,1504,1504,1504,1504,1504,1504,1504,1504,1504,"
        "3514,3514,3514,3514,3514,3514,3514,3514,3514,3514,"
        "2517,2517,2517,2517,2517,2517,2517,2517,2517,2517",
    }

    def __init__(
        self, gamecore_path, runtime_path, runtime_id=0, num_player=2, synch_mode=False
    ):
        self.runtime_path = runtime_path
        self.runtime_id = runtime_id
        self.config_content = []
        self.hero_info = [{} for _ in range(num_player)]
        self.num_player = num_player
        self.default_info = [{} for _ in range(num_player)]
        self.synch_mode = synch_mode
        self.common_config = {
            "ip": "127.0.0.1",
            "init_abs": "1V1.abs",
            "request_freq": 3,
            "timeout": 1000,  # 1s
            "game_id": None,
        }

        self.load_config(config_path=gamecore_path)

    def load_config(self, config_path):
        print("load config...")
        config_path = os.path.join(config_path, "sgame_simulator.conf")
        assert os.path.exists(config_path), "default config {} not exist".format(
            config_path
        )

        # load config info
        self.config_content = []
        self.hero_info = []
        self.default_info = []
        with open(config_path, "r") as f:
            self.config_content = f.readlines()
        for i in range(self.num_player):
            tmp = self.config_content[i + 2].split()
            tmp[0] = re.findall(r"([0-9]*)\[skill=([0-9]*)\]", tmp[0])[0]
            print(self.config_content[i + 2], tmp, config_path)
            hero = tmp[0][0]
            skill = tmp[0][1]
            ip = tmp[1]
            port = tmp[2]
            timeout = tmp[3]
            info_dict = {
                "hero": hero,
                "skill": skill,
                # "ip": ip,
                "port": port,
                # "timeout": timeout,
                "use_common_ai": False,
            }
            # print("info_dict", info_dict)
            self.hero_info.append(info_dict)
            self.default_info.append(info_dict.copy())

    def update_config(self, config_dicts, common_config):
        for i in range(self.num_player):
            for k in self.default_info[i]:
                if k in config_dicts[i]:
                    v = config_dicts[i][k]
                    if k == "hero":
                        v = self.HERO_DICT[v]
                    elif k == "skill":
                        v = self.SKILL_DICT[v]
                    self.hero_info[i][k] = v
                else:
                    self.hero_info[i][k] = self.default_info[i][k]

        for k in self.common_config:
            if k in common_config:
                self.common_config[k] = common_config[k]

    def _set_request(self):
        if self.synch_mode:
            main = None
            for i in range(self.num_player):
                if not self.hero_info[i]["use_common_ai"]:
                    if main is None:
                        self.hero_info[i]["request"] = -1
                        main = i
                    else:
                        self.hero_info[i]["request"] = main

        else:
            for i in range(self.num_player):
                self.hero_info[i]["request"] = -1

    def dump_config(self, config_dicts, common_config=None):
        # if use_common_ai is None:
        #     use_common_ai = [False] * self.num_player

        config_path = os.path.join(self.runtime_path, "sgame_simulator.conf")

        print("dump config...", config_dicts)
        self.update_config(config_dicts, common_config)

        common_config = self.common_config

        # update request_freq.txt
        with open(os.path.join(self.runtime_path, "request_freq.txt"), "w") as f:
            f.write(str(common_config["request_freq"]))

        # update config
        self._set_request()
        with open(config_path, "w") as f:
            f.write("{}\n".format(self.num_player))
            f.write(self.common_config["init_abs"] + "\n")
            for i in range(self.num_player):
                info = self.hero_info[i].copy()
                if info.get("use_common_ai"):
                    # str = "{}[skill={}]\t\t\t{}".format(info["hero"], info["skill"], info["timeout"])
                    s = "{}[skill={} symbol={}]".format(
                        info["hero"], info["skill"], self.SYMBOL[info["hero"]]
                    )
                elif info["request"] != -1:
                    s = "{}[skill={} symbol={} request={}]".format(
                        info["hero"],
                        info["skill"],
                        self.SYMBOL[info["hero"]],
                        info["request"],
                    )
                else:
                    s = "{}[skill={} symbol={}]\t{}\t{}\t{}".format(
                        info["hero"],
                        info["skill"],
                        self.SYMBOL[info["hero"]],
                        common_config["ip"],
                        info["port"],
                        common_config["timeout"],
                    )
                f.write(s + "\n")
