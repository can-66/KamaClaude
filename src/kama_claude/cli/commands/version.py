import kama_claude

# 这是最短的 CLI 示例：不读配置、不连 daemon，只打印包中的版本常量。

# 打印当前 kama_claude 包的版本号
def cmd_version() -> None:
    print(kama_claude.__version__)
