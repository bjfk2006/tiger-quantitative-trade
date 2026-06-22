# -*- coding: utf-8 -*-
"""配置加载：复用 SDK 的 get_client_config，并装配 StrategyConfig.

设计文档 §3 ConfigLoader / §8 凭证经 SDK 三级加载（参数>env>props），
不在本程序硬编码、不入日志、不入快照。
"""
import logging

from tigeropen.tiger_open_config import get_client_config

from option_bot.domain.models import StrategyConfig

logger = logging.getLogger('option_bot.config')


def load_client_config(private_key_path=None, tiger_id=None, account=None,
                       props_path=None, sandbox_debug=False, **kwargs):
    """构造 tigeropen 客户端配置。

    凭证优先级沿用 SDK：参数 > 环境变量 TIGEROPEN_* > 配置文件。
    首期跑模拟盘：account 传入 paper account 即可（SDK 依 account/license 选域名）。
    """
    config = get_client_config(
        private_key_path=private_key_path,
        tiger_id=tiger_id,
        account=account,
        props_path=props_path,
        sandbox_debug=sandbox_debug,
        **kwargs,
    )
    return config


def load_client_config_from_env(props_path=None):
    """纯环境变量/配置文件方式构造客户端配置（服务/容器用）。

    直接用 SDK 的 TigerOpenClientConfig——它在构造时从 TIGEROPEN_* 读取
    tiger_id/account/private_key(内容或路径) 等，避免 get_client_config 强制
    read_private_key(None) 在无 --private-key 时报错。
    """
    from tigeropen.tiger_open_config import TigerOpenClientConfig
    return TigerOpenClientConfig(props_path=props_path)


def load_strategy_config(**overrides) -> StrategyConfig:
    """从 CLI/字典覆盖项构造并校验策略配置。

    只接受 StrategyConfig 已声明的字段，未知键忽略并告警，避免静默吞配置。
    """
    valid = StrategyConfig.__dataclass_fields__.keys()
    clean = {}
    for k, v in overrides.items():
        if v is None:
            continue
        if k in valid:
            clean[k] = v
        else:
            logger.warning('忽略未知策略配置项: %s', k)
    cfg = StrategyConfig(**clean)
    cfg.validate()
    return cfg
