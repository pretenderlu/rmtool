import sys
import subprocess
import pkg_resources
import os
import paramiko
import logging
from getpass import getpass
from datetime import datetime
import json
import shutil

def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

required_packages = {'paramiko'}
installed_packages = {pkg.key for pkg in pkg_resources.working_set}
missing_packages = required_packages - installed_packages

if missing_packages:
    print("正在安装所需的库...")
    for package in missing_packages:
        print(f"安装 {package}...")
        install(package)
    print("所需的库安装完成。")

logging.basicConfig(filename='remarkable_tool.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

CONFIG_FILE = 'config.json'

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {
        'usb': {'host': '10.11.99.1', 'password': None},
        'wifi': {'host': None, 'password': None},
        'FONT_PATH': "/usr/share/fonts/ttf/noto/",
        'WALLPAPER_PATH': "/usr/share/remarkable/suspended.png"
    }

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

def get_connection_info(config):
    while True:
        connection_type = input("请选择连接方式 (1: USB, 2: WiFi): ")
        if connection_type in ['1', '2']:
            break
        print("无效的选择，请重新输入。")

    if connection_type == '1':
        if not config['usb']['password']:
            config['usb']['password'] = getpass("请输入USB连接的root密码: ")
            save_config(config)
        return config['usb']
    else:
        if config['wifi']['host'] and config['wifi']['password']:
            use_saved = input("是否使用保存的WiFi连接信息? (y/n): ").lower() == 'y'
            if use_saved:
                return config['wifi']

        config['wifi']['host'] = input("请输入reMarkable设备的IP地址: ")
        config['wifi']['password'] = getpass("请输入reMarkable设备的root密码: ")
        save_config(config)
        return config['wifi']

def get_ssh_client(connection_info):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(connection_info['host'], username='root', password=connection_info['password'])
        return client
    except Exception as e:
        logging.error(f"SSH连接失败: {str(e)}")
        print(f"SSH连接失败: {str(e)}")
        return None

def execute_command(client, command):
    try:
        stdin, stdout, stderr = client.exec_command(command)
        return stdout.read().decode(), stderr.read().decode()
    except Exception as e:
        logging.error(f"执行命令失败: {str(e)}")
        print(f"执行命令失败: {str(e)}")
        return None, str(e)

def set_rw_mode(client):
    logging.info("切换到读写模式")
    print("正在切换到读写模式...")
    out, err = execute_command(client, "mount -o remount,rw /")
    if err:
        logging.error(f"切换到读写模式失败: {err}")
        print(f"切换到读写模式失败: {err}")
        return False
    print("已切换到读写模式")
    return True

def set_ro_mode(client):
    logging.info("切换回只读模式")
    print("正在切换回只读模式...")
    out, err = execute_command(client, "mount -o remount,ro /")
    if err:
        logging.error(f"切换回只读模式失败: {err}")
        print(f"切换回只读模式失败: {err}")
        return False
    
    out, err = execute_command(client, "mount | grep ' / '")
    if "ro," in out:
        print("已成功切换回只读模式")
        return True
    else:
        logging.error("切换回只读模式失败：mount 命令未显示预期结果")
        print("切换回只读模式失败：mount 命令未显示预期结果")
        return False

def select_file(file_type, folder, extension):
    files = [f for f in os.listdir(folder) if f.endswith(extension)]
    if not files:
        logging.warning(f"未找到{file_type}文件")
        print(f"未找到{file_type}文件")
        return None
    
    print(f"\n可用的{file_type}文件：")
    for i, file in enumerate(files, 1):
        print(f"{i}: {file}")
    
    while True:
        try:
            choice = input(f"\n请选择要使用的{file_type}文件编号 (或按 Enter 取消): ")
            if choice == "":
                return None
            choice = int(choice)
            if 1 <= choice <= len(files):
                selected_file = os.path.join(folder, files[choice-1])
                logging.info(f"选择了文件: {selected_file}")
                return selected_file
            else:
                print("无效的选择，请重试")
        except ValueError:
            print("请输入有效的数字")

def check_file_exists(client, remote_path):
    try:
        sftp = client.open_sftp()
        sftp.stat(remote_path)
        sftp.close()
        return True
    except IOError:
        return False

def transfer_file(client, local_path, remote_path):
    try:
        sftp = client.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.close()
        logging.info(f"文件传输成功: {local_path} -> {remote_path}")
        print(f"文件传输成功: {local_path} -> {remote_path}")
        return True
    except Exception as e:
        logging.error(f"文件传输失败: {str(e)}")
        print(f"文件传输失败: {str(e)}")
        print(f"本地路径: {local_path}")
        print(f"远程路径: {remote_path}")
        return False

def transfer_fonts(client, config):
    logging.info("开始传输字体文件")
    print("正在传输字体文件...")
    
    while True:
        font_file = select_file("字体", "fonts", ".ttf")
        if not font_file:
            return
        
        rename_choice = input("是否要将字体文件重命名为 zwzt.ttf？(Y/N): ").lower()
        new_font_name = "zwzt.ttf" if rename_choice == 'y' else os.path.basename(font_file)
        
        temp_dir = os.path.join("fonts", "temp")
        os.makedirs(temp_dir, exist_ok=True)
        temp_font_path = os.path.join(temp_dir, new_font_name)
        shutil.copy(font_file, temp_font_path)
        print(f"字体文件已复制到 {temp_font_path}")
        
        if set_rw_mode(client):
            out, err = execute_command(client, f"mkdir -p {config['FONT_PATH']}")
            if err:
                logging.error(f"创建目录失败: {err}")
                print(f"创建目录失败: {err}")
            else:
                remote_path = os.path.join(config["FONT_PATH"], new_font_name)
                if transfer_file(client, temp_font_path, remote_path):
                    print("字体文件传输完成")
                    if check_file_exists(client, remote_path):
                        print(f"确认文件已成功上传到 {remote_path}")
                    else:
                        print(f"警告：文件似乎未成功上传到 {remote_path}")
                else:
                    print("字体文件传输失败，请检查设备上的目标路径是否正确")
            
            if not set_ro_mode(client):
                print("警告：切换回只读模式失败，请手动检查设备状态")
        
        os.remove(temp_font_path)
        print(f"临时文件 {new_font_name} 已删除")
        
        if input("是否传输更多字体文件？(Y/N): ").lower() != 'y':
            break

def change_wallpaper(client, config):
    logging.info("开始更换壁纸")
    print("正在更换壁纸...")
    
    wallpaper_file = select_file("壁纸", "wallpaper", ".png")
    if not wallpaper_file:
        return
    
    if set_rw_mode(client):
        print("备份当前壁纸...")
        execute_command(client, f"cp {config['WALLPAPER_PATH']} {config['WALLPAPER_PATH']}.backup")
        
        if transfer_file(client, wallpaper_file, config["WALLPAPER_PATH"]):
            print("壁纸更换完成")
        set_ro_mode(client)

def restart_device(client):
    logging.info("开始重启设备")
    print("正在重启设备...")
    
    confirm = input("确定要重启设备吗？这将断开连接。(Y/N): ")
    if confirm.lower() == 'y':
        out, err = execute_command(client, "reboot")
        if err:
            logging.error(f"重启设备失败: {err}")
            print(f"重启设备失败: {err}")
        else:
            logging.info("设备重启命令已发送")
            print("设备重启命令已发送。请等待设备重新启动。")
            return True
    else:
        print("取消重启操作")
    return False

def check_device_time(client):
    out, err = execute_command(client, "timedatectl")
    if err:
        print(f"获取设备时间失败: {err}")
    else:
        print("设备当前时间信息:")
        print(out)

def set_device_time(client):
    print("设置设备时间...")
    
    out, err = execute_command(client, "timedatectl set-ntp 0")
    if err:
        print(f"禁用NTP失败: {err}")
        return

    new_time = input("请输入新的时间 (格式: YYYY-MM-DD HH:MM:SS): ")
    out, err = execute_command(client, f"timedatectl set-time '{new_time}'")
    if err:
        print(f"设置时间失败: {err}")
    else:
        print("时间设置成功")

    out, err = execute_command(client, "timedatectl set-ntp 1")
    if err:
        print(f"重新启用NTP失败: {err}")
    
    check_device_time(client)

def manage_device_time(client):
    while True:
        print("\n设备时间管理")
        print("1. 查看当前时间")
        print("2. 设置设备时间")
        print("3. 返回主菜单")
        
        choice = input("请选择操作 (1-3): ")
        
        if choice == '1':
            check_device_time(client)
        elif choice == '2':
            set_device_time(client)
        elif choice == '3':
            break
        else:
            print("无效的选择，请重试")

def enable_ssh_over_wlan(client):
    logging.info("启用SSH over WLAN")
    print("正在启用SSH over WLAN...")
    
    out, err = execute_command(client, "rm-ssh-over-wlan on")
    if err:
        logging.error(f"启用SSH over WLAN失败: {err}")
        print(f"启用SSH over WLAN失败: {err}")
    else:
        print("SSH over WLAN 已成功启用")
        print("输出:", out)

def main_menu():
    config = load_config()
    connection_info = get_connection_info(config)
    client = None

    while True:
        print("\nreMarkable 管理工具")
        print("====================")
        print("1. 传输字体文件")
        print("2. 更换壁纸")
        print("3. 管理设备时间")
        print("4. 重启设备")
        print("5. 启用SSH over WLAN")
        print("6. 切换连接方式")
        print("0. 退出")
        
        choice = input("请选择操作 (0-6): ")
        
        if choice == '0':
            break
        
        if not client:
            client = get_ssh_client(connection_info)
            if not client:
                continue
        
        if choice == '1':
            transfer_fonts(client, config)
        elif choice == '2':
            change_wallpaper(client, config)
        elif choice == '3':
            manage_device_time(client)
        elif choice == '4':
            if restart_device(client):
                client.close()
                client = None
        elif choice == '5':
            enable_ssh_over_wlan(client)
        elif choice == '6':
            connection_info = get_connection_info(config)
            if client:
                client.close()
            client = get_ssh_client(connection_info)
        else:
            print("无效的选择，请重试")
    
    if client:
        client.close()
    print("感谢使用，再见！")

if __name__ == "__main__":
    logging.info("脚本开始执行")
    main_menu()
    logging.info("脚本执行结束")
