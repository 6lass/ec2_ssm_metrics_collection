#!/usr/bin/python

from ansible.module_utils.basic import AnsibleModule
import boto3
import time
import re


def run_module():
    module_args = dict(
        region=dict(type='str', required=True),
        instance_id=dict(type='str', required=True, pattern='^i-[a-f0-9]{8,}$'),
        os_type=dict(type='str', required=True, choices=["linux", "windows"])
    )

    result = dict(
        changed=False,
        metrics={}
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=False
    )

    ec2_id = module.params["instance_id"]
    region = module.params["region"]
    os_type = module.params["os_type"]

    try:
        ssm = boto3.client('ssm', region_name=region)

        commands = []
        if os_type == "linux":
            commands = [
                "mpstat 1 1 | awk '/Average/ {print 100 - $NF}'",
                "nproc",
                "ps -eo pid,ppid,cmd,%mem,%cpu --sort=-%cpu -w | head -n 11",
                "echo '---DISK---'",
                "df -h",
                "echo '---RAM---'",
                "free -m"
            ]
        elif os_type == "windows":
            commands = [
                "(Get-Counter '\\Processor(_Total)\\% Processor Time').CounterSamples[0].CookedValue",
                "(Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors",
                "Write-Output '---PROCESSES---'",
                "Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Id,ProcessName,CPU,WS | Format-Table -AutoSize -HideTableHeaders",
                "Write-Output '---DISK---'",
                "Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -eq 3 } | ForEach-Object { \"$($_.DeviceID) $($_.Size - $_.FreeSpace) $($_.FreeSpace) $($_.Size)\" }",
                "Write-Output '---RAM---'",
                "Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize,FreePhysicalMemory"
            ]

        response = ssm.send_command(
            InstanceIds=[ec2_id],
            DocumentName="AWS-RunPowerShellScript" if os_type == "windows" else "AWS-RunShellScript",
            Parameters={"commands": commands},
        )

        command_id = response['Command']['CommandId']

        for _ in range(60):
            time.sleep(5)
            try:
                invocation = ssm.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=ec2_id
                )
                if invocation['Status'] in ['Success', 'Failed', 'Cancelled', 'TimedOut']:
                    break
            except ssm.exceptions.InvocationDoesNotExist:
                time.sleep(2)
                continue

        if invocation['Status'] != 'Success':
            module.fail_json(msg="SSM command failed", details=invocation)

        raw_output = invocation['StandardOutputContent']
        result['metrics'] = parse_output(raw_output, os_type)

        module.exit_json(**result)

    except Exception as e:
        module.fail_json(msg=f"Unexpected error: {str(e)}")


def parse_output(output, os_type):
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    metrics = {
        "cpu_usage": "",
        "cpu_cores": 0,
        "top_cpu_processes": [],
        "disk_usage": [],
        "ram_usage": {}
    }

    try:
        if os_type == "linux":
            cpu_usage_line = None
            cpu_cores_line = None
            pid_header_index = -1
            disk_marker_index = -1
            ram_marker_index = -1

            for i, line in enumerate(lines):
                if i == 0:
                    cpu_usage_line = line
                elif i == 1:
                    cpu_cores_line = line

                if line.strip().startswith("PID") and pid_header_index == -1:
                    pid_header_index = i
                elif "---DISK---" in line and disk_marker_index == -1:
                    disk_marker_index = i
                elif "---RAM---" in line and ram_marker_index == -1:
                    ram_marker_index = i

            if cpu_usage_line is None or cpu_cores_line is None or pid_header_index == -1 or disk_marker_index == -1 or ram_marker_index == -1 or not (pid_header_index > 1 and disk_marker_index > pid_header_index and ram_marker_index > disk_marker_index):
                metrics["error"] = "Error parsing Linux output: Could not find all required markers or they are out of order."
                return metrics

            metrics["cpu_usage"] = cpu_usage_line + "%"
            try:
                metrics["cpu_cores"] = int(cpu_cores_line)
            except ValueError:
                metrics["cpu_cores"] = 0

            process_start_index = pid_header_index + 1
            process_end_index = min(pid_header_index + 1 + 10, disk_marker_index)
            process_lines = lines[process_start_index:process_end_index]

            disk_start_index = disk_marker_index + 1
            disk_lines = lines[disk_start_index:ram_marker_index]

            ram_start_index = ram_marker_index + 1
            ram_lines = lines[ram_start_index:]

            ram_usage_data = parse_linux_ram_usage(ram_lines)
            metrics["ram_usage"] = ram_usage_data
            total_ram_gb = ram_usage_data.get("total_gb", 0.0)

            metrics["top_cpu_processes"] = get_linux_top_cpu_processes(process_lines, total_ram_gb)
            metrics["disk_usage"] = parse_linux_disk_usage(disk_lines)

        elif os_type == "windows":
            cpu_usage_line = None
            cpu_cores_line = None
            proc_marker_index = -1
            disk_marker_index = -1
            ram_marker_index = -1

            for i, line in enumerate(lines):
                if i == 0:
                    cpu_usage_line = line
                elif i == 1:
                    cpu_cores_line = line

                if "---PROCESSES---" in line and proc_marker_index == -1:
                    proc_marker_index = i
                elif "---DISK---" in line and disk_marker_index == -1:
                    disk_marker_index = i
                elif "---RAM---" in line and ram_marker_index == -1:
                    ram_marker_index = i

            if cpu_usage_line is None or cpu_cores_line is None or proc_marker_index == -1 or disk_marker_index == -1 or ram_marker_index == -1 or not (proc_marker_index > 1 and disk_marker_index > proc_marker_index and ram_marker_index > disk_marker_index):
                metrics["error"] = "Error parsing Windows output: Could not find all required markers or they are out of order."
                return metrics

            metrics["cpu_usage"] = f"{round(float(cpu_usage_line), 2)}%"
            try:
                metrics["cpu_cores"] = int(cpu_cores_line)
            except ValueError:
                metrics["cpu_cores"] = 0

            ram_start_index = ram_marker_index + 1
            ram_lines = lines[ram_start_index:]
            ram_usage_data = parse_windows_ram_usage(ram_lines)
            metrics["ram_usage"] = ram_usage_data
            total_ram_bytes = ram_usage_data.get("total_bytes", 0)

            proc_start_index = proc_marker_index + 1
            proc_lines = lines[proc_start_index:disk_marker_index]
            metrics["top_cpu_processes"] = get_windows_top_cpu_processes(proc_lines, total_ram_bytes)

            disk_start_index = disk_marker_index + 1
            disk_lines = lines[disk_start_index:ram_marker_index]
            metrics["disk_usage"] = parse_windows_disk_usage(disk_lines)

    except Exception as e:
        metrics["error"] = f"General parsing error: {str(e)}"

    return metrics


def get_linux_top_cpu_processes(lines, total_ram_gb):
    processes = []
    for line in lines:
        parts = re.split(r'\s+', line.strip(), maxsplit=4)
        if len(parts) == 5:
            pid, ppid, cmd, mem_percent_str, cpu_percent_str = parts
            try:
                cpu_percent = float(cpu_percent_str)
                memory_percent = float(mem_percent_str)
                memory_gb = round((memory_percent / 100.0) * total_ram_gb, 3)
            except (ValueError, TypeError):
                cpu_percent = 0.0
                memory_percent = 0.0
                memory_gb = 0.0

            processes.append({
                "cpu_percent": cpu_percent,
                "memory_percent": memory_percent,
                "memory_gb": memory_gb,
                "name": cmd.strip(),
                "pid": pid,
                "ppid": ppid
            })
    return processes


def get_windows_top_cpu_processes(lines, total_ram_bytes):
    processes = []
    for line in lines:
        parts = line.split(None, 3)
        if len(parts) == 4:
            pid, name, cpu_str, ws_bytes_str = parts
            try:
                cpu_percent = round(float(cpu_str), 2) if cpu_str != 'N/A' else 0.0
            except ValueError:
                cpu_percent = 0.0

            try:
                ws_bytes = int(ws_bytes_str)
                memory_gb = round(ws_bytes / (1024 ** 3), 3)
                memory_percent = round((ws_bytes / total_ram_bytes) * 100.0, 2) if total_ram_bytes > 0 else 0.0
            except (ValueError, TypeError, ZeroDivisionError):
                ws_bytes = int(ws_bytes_str) if ws_bytes_str.isdigit() else 0
                memory_gb = round(ws_bytes / (1024 ** 3), 3) if ws_bytes > 0 else 0.0
                memory_percent = 0.0

            processes.append({
                "cpu_percent": cpu_percent,
                "memory_percent": memory_percent,
                "memory_gb": memory_gb,
                "name": name,
                "pid": pid,
            })
    return processes


def parse_linux_disk_usage(lines):
    details = []
    if lines:
        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 6:
                fs, size, used, avail, use_pct, mount = parts[:6]
                detail = {
                    "filesystem": fs,
                    "size_gb": convert_to_gb(size),
                    "used_gb": convert_to_gb(used),
                    "avail_gb": convert_to_gb(avail),
                    "use_percent": use_pct,
                    "mounted_on": mount
                }
                details.append(detail)

    main_disk = next((d for d in details if d.get("filesystem") == "/dev/root"), details[0] if details else None)
    if main_disk:
        return [{
            "name": main_disk.get("filesystem", "N/A"),
            "free_gb": main_disk.get("avail_gb", 0.0),
            "used_gb": main_disk.get("used_gb", 0.0),
            "total_gb": main_disk.get("size_gb", 0.0),
            "use_percent": main_disk.get("use_percent", "0%"),
            "details": details
        }]
    return details if details else []


def parse_linux_ram_usage(lines):
    for line in lines:
        if line.strip().lower().startswith("mem:"):
            parts = line.split()
            if len(parts) >= 4:
                try:
                    total_mb = int(parts[1])
                    used_mb = int(parts[2])
                    free_mb = int(parts[3])
                    return {
                        "total_gb": round(total_mb / 1024, 2),
                        "used_gb": round(used_mb / 1024, 2),
                        "free_gb": round(free_mb / 1024, 2),
                        "total_bytes": total_mb * 1024 * 1024
                    }
                except ValueError:
                    return {}
    return {}


def parse_windows_disk_usage(lines):
    details = []
    for line in lines:
        parts = line.split()
        if len(parts) == 4:
            name, used_str, free_str, size_str = parts
            try:
                used_gb = round(int(used_str)/(1024**3), 2)
                free_gb = round(int(free_str)/(1024**3), 2)
                size_gb = round(int(size_str)/(1024**3), 2)
                percent = f"{round((used_gb / size_gb) * 100, 1)}%" if size_gb > 0 else "0%"
            except (ValueError, ZeroDivisionError):
                used_gb = free_gb = size_gb = 0.0
                percent = "0%"

            detail = {
                "filesystem": name,
                "size_gb": size_gb,
                "used_gb": used_gb,
                "avail_gb": free_gb,
                "use_percent": percent,
                "mounted_on": name
            }
            details.append(detail)

    main_disk = next((d for d in details if d.get("filesystem") == "C:"), details[0] if details else None)
    if main_disk:
        return [{
            "name": main_disk.get("filesystem", "N/A"),
            "free_gb": main_disk.get("avail_gb", 0.0),
            "used_gb": main_disk.get("used_gb", 0.0),
            "total_gb": main_disk.get("size_gb", 0.0),
            "use_percent": main_disk.get("use_percent", "0%"),
            "details": details
        }]
    return details if details else []


def parse_windows_ram_usage(lines):
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            try:
                total_kb = int(parts[0])
                free_kb = int(parts[1])
                used_kb = total_kb - free_kb
                return {
                    "total_gb": round(total_kb / (1024 * 1024), 2),
                    "used_gb": round(used_kb / (1024 * 1024), 2),
                    "free_gb": round(free_kb / (1024 * 1024), 2),
                    "total_bytes": total_kb * 1024
                }
            except ValueError:
                return {}
    return {}


def convert_to_gb(value_str):
    try:
        value_str = value_str.replace(',', '')
        units = {'G': 1024**3, 'M': 1024**2, 'K': 1024**1}
        unit_factor = 1

        for unit, factor in units.items():
            if value_str.upper().endswith(unit):
                value_str = value_str[:-1]
                unit_factor = factor
                break

        numeric_value_bytes = float(value_str) * unit_factor
        return round(numeric_value_bytes / (1024**3), 2)

    except (ValueError, AttributeError):
        return 0.0


def main():
    run_module()


if __name__ == '__main__':
    main()