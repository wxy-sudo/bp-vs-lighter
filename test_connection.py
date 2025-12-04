#!/usr/bin/env python3
"""Test script to diagnose Lighter API connection issues."""

import os
import sys
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def test_basic_network():
    """Test basic internet connectivity."""
    print("=" * 60)
    print("1. 测试基本网络连接...")
    try:
        response = requests.get("https://www.google.com", timeout=10)
        print(f"   ✅ Google 连接成功 (status: {response.status_code})")
        return True
    except Exception as e:
        print(f"   ❌ Google 连接失败: {e}")
        return False

def test_lighter_api_basic():
    """Test basic Lighter API connectivity."""
    print("=" * 60)
    print("2. 测试 Lighter API 基本连接...")
    url = "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"
    try:
        response = requests.get(url, timeout=15)
        print(f"   ✅ Lighter API 连接成功 (status: {response.status_code})")
        if response.status_code == 200:
            data = response.json()
            print(f"   📊 找到 {len(data.get('order_books', []))} 个交易对")
        return True
    except requests.exceptions.Timeout:
        print(f"   ❌ Lighter API 连接超时")
        return False
    except Exception as e:
        print(f"   ❌ Lighter API 连接失败: {e}")
        return False

def test_lighter_apikeys_endpoint():
    """Test the specific API keys endpoint that's failing."""
    print("=" * 60)
    print("3. 测试 Lighter API Keys 端点...")
    
    account_index = os.getenv('LIGHTER_ACCOUNT_INDEX')
    api_key_index = os.getenv('LIGHTER_API_KEY_INDEX')
    
    if not account_index or not api_key_index:
        print("   ❌ 环境变量未设置: LIGHTER_ACCOUNT_INDEX 或 LIGHTER_API_KEY_INDEX")
        return False
    
    url = f"https://mainnet.zklighter.elliot.ai/api/v1/apikeys?account_index={account_index}&api_key_index={api_key_index}"
    print(f"   URL: {url}")
    
    try:
        response = requests.get(url, timeout=15)
        print(f"   状态码: {response.status_code}")
        print(f"   响应内容: {response.text[:500]}")
        return response.status_code == 200
    except requests.exceptions.Timeout:
        print(f"   ❌ API Keys 端点连接超时")
        return False
    except Exception as e:
        print(f"   ❌ API Keys 端点连接失败: {e}")
        return False

def test_env_variables():
    """Check environment variables."""
    print("=" * 60)
    print("4. 检查环境变量...")
    
    required_vars = [
        'BACKPACK_PUBLIC_KEY',
        'BACKPACK_SECRET_KEY',
        'API_KEY_PRIVATE_KEY',
        'LIGHTER_ACCOUNT_INDEX',
        'LIGHTER_API_KEY_INDEX'
    ]
    
    all_set = True
    for var in required_vars:
        value = os.getenv(var)
        if value:
            # 只显示部分内容，保护敏感信息
            display_value = value[:10] + "..." if len(value) > 10 else value
            print(f"   ✅ {var} = {display_value}")
        else:
            print(f"   ❌ {var} 未设置")
            all_set = False
    
    return all_set

def test_lighter_sdk_import():
    """Test if lighter SDK can be imported."""
    print("=" * 60)
    print("5. 测试 Lighter SDK 导入...")
    
    try:
        from lighter.signer_client import SignerClient
        print("   ✅ Lighter SDK 导入成功")
        return True
    except ImportError as e:
        print(f"   ❌ Lighter SDK 导入失败: {e}")
        print("   💡 建议: 运行以下命令安装 Lighter SDK:")
        print("      cd ../lighter-python-main && pip install -e .")
        return False

async def test_lighter_client_creation_async():
    """Test Lighter client creation in async context."""
    try:
        from lighter.signer_client import SignerClient
        
        api_key_private_key = os.getenv('API_KEY_PRIVATE_KEY')
        account_index = int(os.getenv('LIGHTER_ACCOUNT_INDEX', '0'))
        api_key_index = int(os.getenv('LIGHTER_API_KEY_INDEX', '0'))
        
        if not api_key_private_key:
            print("   ❌ API_KEY_PRIVATE_KEY 环境变量未设置")
            return False
        
        print(f"   账户索引: {account_index}")
        print(f"   API Key 索引: {api_key_index}")
        
        # 尝试创建客户端
        client = SignerClient(
            url="https://mainnet.zklighter.elliot.ai",
            private_key=api_key_private_key,
            account_index=account_index,
            api_key_index=api_key_index,
        )
        
        print("   ✅ Lighter 客户端创建成功")
        
        # 现在尝试 check_client
        print("   正在验证客户端...")
        err = client.check_client()
        if err is not None:
            print(f"   ❌ check_client 失败: {err}")
            return False
        
        print("   ✅ check_client 验证成功")
        return True
        
    except Exception as e:
        print(f"   ❌ 客户端创建/验证失败: {e}")
        import traceback
        print(f"   详细错误: {traceback.format_exc()}")
        return False

def test_lighter_client_creation():
    """Test Lighter client creation (wrapper for async function)."""
    print("=" * 60)
    print("6. 测试 Lighter 客户端创建...")
    
    import asyncio
    try:
        return asyncio.run(test_lighter_client_creation_async())
    except Exception as e:
        print(f"   ❌ 异步测试失败: {e}")
        return False

def main():
    print("\n" + "=" * 60)
    print("  Lighter API 连接诊断工具")
    print("=" * 60 + "\n")
    
    results = {
        "基本网络": test_basic_network(),
        "Lighter API 基本连接": test_lighter_api_basic(),
        "API Keys 端点": test_lighter_apikeys_endpoint(),
        "环境变量": test_env_variables(),
        "SDK 导入": test_lighter_sdk_import(),
        "客户端验证": test_lighter_client_creation(),
    }
    
    print("\n" + "=" * 60)
    print("  诊断结果汇总")
    print("=" * 60)
    
    all_passed = True
    for name, result in results.items():
        status = "✅" if result else "❌"
        print(f"   {status} {name}")
        if not result:
            all_passed = False
    
    print("\n" + "=" * 60)
    if all_passed:
        print("  🎉 所有测试通过！可以运行套利脚本")
    else:
        print("  ⚠️ 部分测试失败，请根据上面的错误信息排查问题")
        print("\n  常见解决方案:")
        print("  1. 如果是网络问题，尝试:")
        print("     - 检查 VPN/代理设置")
        print("     - 在 WSL 中运行: sudo service dns restart")
        print("     - 检查 /etc/resolv.conf 中的 DNS 设置")
        print("  2. 如果是 SDK 问题，运行:")
        print("     cd ../lighter-python-main && pip install -e .")
        print("  3. 如果是 API Key 问题:")
        print("     - 确认 .env 文件中的配置正确")
        print("     - 检查 Lighter 账户状态")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    main()

