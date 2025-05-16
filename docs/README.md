# EC2 SSM Metrics Collection

Ansible collection to gather EC2 instance metrics using AWS Systems Manager.

## Requirements

-  Python 3.6+
-  boto3
-  Ansible 2.9+

## Installation

```bash
ansible-galaxy collection install git+https://github.com/yourusername/ec2_ssm_metrics_collection.git
```

## Module: `ec2_ssm_metrics`

### Parameters

| Parameter     | Required | Type | Description                     |
| ------------- | -------- | ---- | ------------------------------- |
| `region`      | Yes      | str  | AWS region (e.g., "us-east-1")  |
| `instance_id` | Yes      | str  | EC2 instance ID (e.g., "i-123") |
| `os_type`     | Yes      | str  | OS type ("linux" or "windows")  |

### Example

```yaml
- name: Get Linux metrics
  ec2_ssm_metrics:
     region: "us-east-1"
     instance_id: "i-1234567890"
     os_type: "linux"
  register: metrics
```
