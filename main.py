import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import aiomysql
import subprocess
import platform
import json
import os
from datetime import datetime
import logging
from dotenv import load_dotenv
import socket
from urllib.parse import urlparse

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('DISCORD_TOKEN')
ALERT_CHANNEL_ID = int(os.getenv('ALERT_CHANNEL_ID', 0)) if os.getenv('ALERT_CHANNEL_ID') else None
MYSQL_URL = os.getenv('MYSQL_URL')

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

db_pool = None
service_statuses = {}
status_message_id = None
status_channel_id = None

class DatabaseManager:
    def __init__(self, mysql_url):
        self.mysql_url = mysql_url
        
    async def create_pool(self):
        url_parts = self.mysql_url.replace('mysql://', '').split('/')
        database = url_parts[1] if len(url_parts) > 1 else 'monitor_bot'
        user_host = url_parts[0].split('@')
        user_pass = user_host[0].split(':')
        host_port = user_host[1].split(':')
        
        user = user_pass[0]
        password = user_pass[1] if len(user_pass) > 1 else ''
        host = host_port[0]
        port = int(host_port[1]) if len(host_port) > 1 else 3306
        
        self.pool = await aiomysql.create_pool(
            host=host,
            port=port,
            user=user,
            password=password,
            db=database,
            autocommit=True,
            maxsize=10
        )
        
        await self.create_tables()
        
    async def create_tables(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS services (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        name VARCHAR(255) UNIQUE NOT NULL,
                        url VARCHAR(500),
                        host VARCHAR(255),
                        port INT,
                        service_type ENUM('http', 'tcp', 'ping') NOT NULL,
                        timeout_seconds INT DEFAULT 5,
                        max_retries INT DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        is_active BOOLEAN DEFAULT TRUE
                    )
                """)
                
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS service_status_history (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        service_id INT,
                        status ENUM('up', 'down', 'unknown') NOT NULL,
                        response_time DECIMAL(10,4),
                        error_message TEXT,
                        checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE
                    )
                """)
                
                try:
                    await cursor.execute("""
                        ALTER TABLE service_status_history 
                        MODIFY COLUMN response_time DECIMAL(10,4)
                    """)
                except Exception:
                    pass
                
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS bot_config (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        config_key VARCHAR(100) UNIQUE NOT NULL,
                        config_value TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    )
                """)
    
    async def add_service(self, name, service_type, url=None, host=None, port=None, timeout=5, max_retries=1):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                try:
                    await cursor.execute("""
                        INSERT INTO services (name, url, host, port, service_type, timeout_seconds, max_retries)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (name, url, host, port, service_type, timeout, max_retries))
                    return cursor.lastrowid
                except aiomysql.IntegrityError:
                    raise ValueError(f"Service '{name}' already exists")
    
    async def remove_service(self, name):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("UPDATE services SET is_active = FALSE WHERE name = %s", (name,))
                return cursor.rowcount > 0
    
    async def get_active_services(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("""
                    SELECT id, name, url, host, port, service_type, timeout_seconds, max_retries
                    FROM services WHERE is_active = TRUE
                """)
                return await cursor.fetchall()
    
    async def save_status_message_id(self, message_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    INSERT INTO bot_config (config_key, config_value) 
                    VALUES ('status_message_id', %s)
                    ON DUPLICATE KEY UPDATE config_value = %s
                """, (str(message_id), str(message_id)))
    
    async def get_status_message_id(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT config_value FROM bot_config WHERE config_key = 'status_message_id'
                """)
                result = await cursor.fetchone()
                return int(result[0]) if result else None
    
    async def save_status_channel_id(self, channel_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    INSERT INTO bot_config (config_key, config_value) 
                    VALUES ('status_channel_id', %s)
                    ON DUPLICATE KEY UPDATE config_value = %s
                """, (str(channel_id), str(channel_id)))
    
    async def get_status_channel_id(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT config_value FROM bot_config WHERE config_key = 'status_channel_id'
                """)
                result = await cursor.fetchone()
                return int(result[0]) if result else None
    
    async def log_service_status(self, service_id, status, response_time=None, error_message=None):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    INSERT INTO service_status_history (service_id, status, response_time, error_message)
                    VALUES (%s, %s, %s, %s)
                """, (service_id, status, response_time, error_message))

db_manager = DatabaseManager(MYSQL_URL)

async def ping_host(host, timeout=5, count=1):
    try:
        if platform.system().lower() == "windows":
            cmd = ["ping", "-n", str(count), "-w", str(timeout * 1000), host]
        else:
            cmd = ["ping", "-c", str(count), "-W", str(timeout), host]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            output = stdout.decode()
            
            import re
            if platform.system().lower() == "windows":
                time_match = re.search(r'time[<=](\d+)ms', output)
            else:
                time_match = re.search(r'time=(\d+\.?\d*)\s*ms', output)
            
            if time_match:
                response_time = float(time_match.group(1)) / 1000
                return {
                    'status': 'up',
                    'response_time': response_time
                }
            else:
                return {
                    'status': 'up',
                    'response_time': None
                }
        else:
            error_output = stderr.decode() if stderr else stdout.decode()
            return {
                'status': 'down',
                'error': f"Ping failed: {error_output.strip()}"
            }
            
    except Exception as e:
        return {
            'status': 'down',
            'error': f"Ping error: {str(e)}"
        }

async def check_http_service(service):
    try:
        url = service['url']
        
        if not url.startswith(('http://', 'https://')):
            url = f"https://{url}"
        
        parsed_url = urlparse(url)
        hostname = parsed_url.hostname
        
        if not hostname:
            clean_url = url.replace('https://', '').replace('http://', '').split('/')[0].split(':')[0]
            hostname = clean_url if clean_url else 'unknown'
            
            if hostname == 'unknown':
                return {
                    'status': 'down',
                    'error': f'Invalid URL format: {service["url"]}',
                    'hostname': 'unknown'
                }
        
        ping_result = await ping_host(hostname, timeout=service['timeout_seconds'])
        
        if ping_result['status'] == 'down':
            return {
                'status': 'down',
                'error': f"Hostname unreachable: {ping_result.get('error', 'Ping failed')}",
                'hostname': hostname
            }
        
        timeout = aiohttp.ClientTimeout(total=service['timeout_seconds'])
        async with aiohttp.ClientSession(timeout=timeout) as session:
            start_time = datetime.now()
            async with session.get(url) as response:
                response_time = (datetime.now() - start_time).total_seconds()
                
                return {
                    'status': 'up',
                    'response_time': response_time,
                    'status_code': response.status,
                    'hostname': hostname,
                    'ping_time': ping_result.get('response_time'),
                    'final_url': url
                }
                
    except Exception as e:
        return {
            'status': 'down',
            'error': str(e),
            'hostname': hostname if 'hostname' in locals() else 'unknown'
        }

async def check_tcp_service(service):
    try:
        ping_result = await ping_host(service['host'], timeout=service['timeout_seconds'])
        
        if ping_result['status'] == 'down':
            return {
                'status': 'down',
                'error': f"Host unreachable: {ping_result.get('error', 'Ping failed')}",
                'hostname': service['host']
            }
        
        start_time = datetime.now()
        future = asyncio.open_connection(service['host'], service['port'])
        reader, writer = await asyncio.wait_for(future, timeout=service['timeout_seconds'])
        response_time = (datetime.now() - start_time).total_seconds()
        writer.close()
        await writer.wait_closed()
        
        return {
            'status': 'up',
            'response_time': response_time,
            'hostname': service['host'],
            'ping_time': ping_result.get('response_time')
        }
        
    except Exception as e:
        return {
            'status': 'down',
            'error': str(e),
            'hostname': service['host']
        }

async def check_ping_service(service):
    result = await ping_host(service['host'], timeout=service['timeout_seconds'])
    result['hostname'] = service['host']
    return result

async def check_service_with_retries(service):
    max_retries = service.get('max_retries', 1)
    
    for attempt in range(max_retries + 1):
        if service['service_type'] == 'http':
            result = await check_http_service(service)
        elif service['service_type'] == 'tcp':
            result = await check_tcp_service(service)
        elif service['service_type'] == 'ping':
            result = await check_ping_service(service)
        else:
            result = {'status': 'unknown', 'error': 'Unknown service type'}
        
        if result['status'] == 'up' or attempt == max_retries:
            return result
        
        if attempt < max_retries:
            await asyncio.sleep(1)
    
    return result

async def check_all_services():
    services = await db_manager.get_active_services()
    results = []
    
    for service in services:
        logger.info(f"Checking {service['name']}...")
        
        result = await check_service_with_retries(service)
        
        service_id = service['id']
        previous_status = service_statuses.get(service['name'], {}).get('status', 'unknown')
        
        service_statuses[service['name']] = {
            'status': result['status'],
            'last_check': datetime.now(),
            'last_response_time': result.get('response_time')
        }
        
        await db_manager.log_service_status(
            service_id,
            result['status'],
            result.get('response_time'),
            result.get('error')
        )
        
        results.append({
            'service': service['name'],
            'service_config': service,
            'status_changed': previous_status != result['status'] and previous_status != 'unknown',
            **result
        })
    
    return results

def create_status_embed(results):
    if not results:
        embed = discord.Embed(
            title="📊 No Services Configured",
            description="Use `/addservice` to add services to monitor",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        return embed
    
    up_count = sum(1 for r in results if r['status'] == 'up')
    down_count = len(results) - up_count
    
    if down_count == 0:
        color = discord.Color.green()
        status_text = "All Systems Operational"
        emoji = "✅"
    elif up_count == 0:
        color = discord.Color.red()
        status_text = "All Systems Down"
        emoji = "🔴"
    else:
        color = discord.Color.orange()
        status_text = "Partial Outage"
        emoji = "⚠️"
    
    embed = discord.Embed(
        title=f"{emoji} {status_text}",
        description=f"**{up_count}** up • **{down_count}** down",
        timestamp=datetime.now(),
        color=color
    )
    
    service_text = ""
    for result in results:
        service_config = result['service_config']
        
        if result['status'] == 'up':
            status_emoji = "🟢"
            
            if service_config['service_type'] == 'http':
                display_url = result.get('final_url', service_config['url'])
                if not display_url.startswith(('http://', 'https://')):
                    display_url = f"https://{display_url}"
                    
                service_info = f"[{display_url}]({display_url})"
                hostname = result.get('hostname', 'unknown')
                
                time_info = ""
                if result.get('ping_time'):
                    time_info += f"ping: {result['ping_time']:.0f}ms"
                if result.get('response_time'):
                    if time_info:
                        time_info += f", http: {result['response_time']:.2f}s"
                    else:
                        time_info = f"http: {result['response_time']:.2f}s"
                
                if time_info:
                    service_text += f"{status_emoji} **{result['service']}**\n   └ {service_info}\n   └ Host: `{hostname}` ({time_info})\n\n"
                else:
                    service_text += f"{status_emoji} **{result['service']}**\n   └ {service_info}\n   └ Host: `{hostname}`\n\n"
                    
            elif service_config['service_type'] == 'tcp':
                hostname = result.get('hostname', service_config['host'])
                service_info = f"`{hostname}:{service_config['port']}`"
                
                time_info = ""
                if result.get('ping_time'):
                    time_info += f"ping: {result['ping_time']:.0f}ms"
                if result.get('response_time'):
                    if time_info:
                        time_info += f", tcp: {result['response_time']:.2f}s"
                    else:
                        time_info = f"tcp: {result['response_time']:.2f}s"
                
                if time_info:
                    service_text += f"{status_emoji} **{result['service']}**\n   └ {service_info} ({time_info})\n\n"
                else:
                    service_text += f"{status_emoji} **{result['service']}**\n   └ {service_info}\n\n"
                    
            elif service_config['service_type'] == 'ping':
                hostname = result.get('hostname', service_config['host'])
                service_info = f"`{hostname}`"
                
                time_info = ""
                if result.get('response_time'):
                    time_info = f"ping: {result['response_time']:.0f}ms"
                
                if time_info:
                    service_text += f"{status_emoji} **{result['service']}**\n   └ {service_info} ({time_info})\n\n"
                else:
                    service_text += f"{status_emoji} **{result['service']}**\n   └ {service_info}\n\n"
                    
        else:
            status_emoji = "🔴"
            error = result.get('error', 'Unknown error')
            hostname = result.get('hostname', 'unknown')
            
            if service_config['service_type'] == 'http':
                display_url = service_config['url']
                if not display_url.startswith(('http://', 'https://')):
                    display_url = f"https://{display_url}"
                service_info = f"[{display_url}]({display_url})"
                service_text += f"{status_emoji} **{result['service']}**\n   └ {service_info}\n   └ Host: `{hostname}` - **{error}**\n\n"
            elif service_config['service_type'] == 'tcp':
                service_info = f"`{hostname}:{service_config['port']}`"
                service_text += f"{status_emoji} **{result['service']}**\n   └ {service_info} - **{error}**\n\n"
            elif service_config['service_type'] == 'ping':
                service_info = f"`{hostname}`"
                service_text += f"{status_emoji} **{result['service']}**\n   └ {service_info} - **{error}**\n\n"
    
    if len(service_text) > 1024:
        services_chunks = service_text.split('\n\n')
        current_chunk = ""
        field_count = 1
        
        for chunk in services_chunks:
            if len(current_chunk + chunk + '\n\n') > 1024:
                embed.add_field(name=f"Services ({field_count})", value=current_chunk, inline=False)
                current_chunk = chunk + '\n\n'
                field_count += 1
            else:
                current_chunk += chunk + '\n\n'
        
        if current_chunk:
            embed.add_field(name=f"Services ({field_count})", value=current_chunk, inline=False)
    else:
        embed.add_field(name="Services", value=service_text, inline=False)
    
    embed.set_footer(text=f"Updates every 5 minutes • Total: {len(results)} services")
    
    return embed

async def update_bot_status(results):
    if not results:
        await bot.change_presence(
            status=discord.Status.idle,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="No services configured"
            )
        )
        return
    
    up_count = sum(1 for r in results if r['status'] == 'up')
    down_count = len(results) - up_count
    total_count = len(results)
    
    if down_count == 0:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"All {total_count} services ✅"
            )
        )
    else:
        await bot.change_presence(
            status=discord.Status.do_not_disturb,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{down_count} services down ⚠️"
            )
        )
    
    logger.info(f"Bot status updated: {up_count} up, {down_count} down")

async def create_or_update_status_message(results):
    global status_message_id, status_channel_id
    
    if not status_channel_id:
        logger.warning("No status channel configured. Use /setchannel to set one.")
        return
    
    channel = bot.get_channel(status_channel_id)
    if not channel:
        logger.error(f"Could not find status channel with ID {status_channel_id}")
        return
    
    embed = create_status_embed(results)
    
    await update_bot_status(results)
    
    if status_message_id:
        try:
            message = await channel.fetch_message(status_message_id)
            await message.edit(embed=embed)
            logger.info("Updated existing status message")
            return
        except discord.NotFound:
            logger.info("Status message not found, creating new one")
            status_message_id = None
        except Exception as e:
            logger.error(f"Error updating status message: {e}")
    
    try:
        status_message = await channel.send(embed=embed)
        status_message_id = status_message.id
        await db_manager.save_status_message_id(status_message_id)
        logger.info(f"Created new status message with ID: {status_message_id}")
            
    except Exception as e:
        logger.error(f"Error creating status message: {e}")

async def send_status_change_alerts(results):
    alert_channel_id = ALERT_CHANNEL_ID
    
    if not alert_channel_id:
        return
        
    changed_services = [r for r in results if r.get('status_changed', False)]
    if not changed_services:
        return
        
    alert_channel = bot.get_channel(alert_channel_id)
    if not alert_channel:
        logger.warning(f"Alert channel {alert_channel_id} not found")
        return
        
    for service in changed_services:
        try:
            service_config = service['service_config']
            
            alert_embed = discord.Embed(
                title="⚠️ Service Status Change",
                description=f"**{service['service']}** is now **{service['status'].upper()}**",
                color=discord.Color.green() if service['status'] == 'up' else discord.Color.red(),
                timestamp=datetime.now()
            )
            
            if service_config['service_type'] == 'http':
                display_url = service_config['url']
                if not display_url.startswith(('http://', 'https://')):
                    display_url = f"https://{display_url}"
                alert_embed.add_field(name="URL", value=f"[{display_url}]({display_url})", inline=False)
                if service.get('hostname'):
                    alert_embed.add_field(name="Hostname", value=f"`{service['hostname']}`", inline=True)
            elif service_config['service_type'] == 'tcp':
                alert_embed.add_field(name="Host:Port", value=f"`{service_config['host']}:{service_config['port']}`", inline=False)
            elif service_config['service_type'] == 'ping':
                alert_embed.add_field(name="Host", value=f"`{service_config['host']}`", inline=False)
            
            if service['status'] == 'down' and service.get('error'):
                alert_embed.add_field(name="Error", value=service['error'], inline=False)
            elif service['status'] == 'up':
                if service.get('response_time'):
                    alert_embed.add_field(name="Response Time", value=f"{service['response_time']:.2f}s", inline=True)
                if service.get('ping_time'):
                    alert_embed.add_field(name="Ping Time", value=f"{service['ping_time']:.0f}ms", inline=True)
            
            await alert_channel.send(embed=alert_embed)
            logger.info(f"Sent alert for {service['service']} status change")
            
        except Exception as e:
            logger.error(f"Error sending alert for {service.get('service', 'unknown')}: {e}")

@bot.event
async def on_ready():
    global status_message_id, status_channel_id, db_pool
    
    logger.info(f'{bot.user} has connected to Discord!')
    
    await bot.change_presence(
        status=discord.Status.idle,
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="Starting up..."
        )
    )
    
    try:
        await db_manager.create_pool()
        logger.info("Database connection established")
        
        status_message_id = await db_manager.get_status_message_id()
        status_channel_id = await db_manager.get_status_channel_id()
        
        if status_message_id:
            logger.info(f"Loaded existing status message ID: {status_message_id}")
        if status_channel_id:
            logger.info(f"Loaded status channel ID: {status_channel_id}")
        else:
            logger.warning("No status channel configured. Use /setchannel to set one.")
        
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        await bot.change_presence(
            status=discord.Status.do_not_disturb,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Database error ❌"
            )
        )
        return
    
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")
    
    if status_channel_id:
        try:
            results = await check_all_services()
            await update_bot_status(results)
        except Exception as e:
            logger.error(f"Initial status check failed: {e}")
    
    monitor_services.start()
    logger.info("Service monitoring started!")

@tasks.loop(minutes=5)
async def monitor_services():
    logger.info("Running scheduled service check...")
    
    try:
        if not status_channel_id:
            logger.info("No status channel configured, skipping monitoring")
            return
            
        results = await check_all_services()
        await create_or_update_status_message(results)
        await send_status_change_alerts(results)
        await update_bot_status(results)
    except NameError as e:
        logger.error(f"NameError in monitoring task: {e}")
        logger.error(f"Available globals: {list(globals().keys())}")
        await bot.change_presence(
            status=discord.Status.do_not_disturb,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Monitoring error ❌"
            )
        )
    except Exception as e:
        logger.error(f"Error in monitoring task: {e}")
        await bot.change_presence(
            status=discord.Status.do_not_disturb,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Monitoring error ❌"
            )
        )

@monitor_services.before_loop
async def before_monitor_services():
    await bot.wait_until_ready()

@bot.tree.command(name="addservice", description="Add a new service to monitor")
async def add_service_command(
    interaction: discord.Interaction,
    name: str,
    url: str,
    timeout: int = 5
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
        return
    
    if timeout < 1 or timeout > 30:
        await interaction.response.send_message("❌ Timeout must be between 1 and 30 seconds", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        service_id = await db_manager.add_service(
            name=name,
            service_type='http',
            url=url,
            host=None,
            port=None,
            timeout=timeout,
            max_retries=1
        )
        
        results = await check_all_services()
        if status_channel_id:
            await create_or_update_status_message(results)
        else:
            logger.warning("Service added but no status channel configured")
        await update_bot_status(results)
        
        channel_warning = "" if status_channel_id else "\n⚠️ Set a status channel with `/setchannel` to see updates"
        await interaction.followup.send(f"✅ Service '{name}' added successfully! (ID: {service_id}){channel_warning}", ephemeral=True)
        
    except ValueError as e:
        await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
    except Exception as e:
        logger.error(f"Error adding service: {e}")
        await interaction.followup.send("❌ Error adding service to database", ephemeral=True)

@bot.tree.command(name="addtcp", description="Add a TCP service to monitor")
async def add_tcp_command(
    interaction: discord.Interaction,
    name: str,
    host: str,
    port: int,
    timeout: int = 5
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
        return
    
    if timeout < 1 or timeout > 30:
        await interaction.response.send_message("❌ Timeout must be between 1 and 30 seconds", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        service_id = await db_manager.add_service(
            name=name,
            service_type='tcp',
            url=None,
            host=host,
            port=port,
            timeout=timeout,
            max_retries=1
        )
        
        results = await check_all_services()
        if status_channel_id:
            await create_or_update_status_message(results)
        else:
            logger.warning("TCP service added but no status channel configured")
        await update_bot_status(results)
        
        channel_warning = "" if status_channel_id else "\n⚠️ Set a status channel with `/setchannel` to see updates"
        await interaction.followup.send(f"✅ TCP service '{name}' added successfully! (ID: {service_id}){channel_warning}", ephemeral=True)
        
    except ValueError as e:
        await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
    except Exception as e:
        logger.error(f"Error adding TCP service: {e}")
        await interaction.followup.send("❌ Error adding TCP service to database", ephemeral=True)

@bot.tree.command(name="addping", description="Add a ping-only service to monitor")
async def add_ping_command(
    interaction: discord.Interaction,
    name: str,
    host: str,
    timeout: int = 5
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
        return
    
    if timeout < 1 or timeout > 30:
        await interaction.response.send_message("❌ Timeout must be between 1 and 30 seconds", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        service_id = await db_manager.add_service(
            name=name,
            service_type='ping',
            url=None,
            host=host,
            port=None,
            timeout=timeout,
            max_retries=1
        )
        
        results = await check_all_services()
        if status_channel_id:
            await create_or_update_status_message(results)
        else:
            logger.warning("Ping service added but no status channel configured")
        await update_bot_status(results)
        
        channel_warning = "" if status_channel_id else "\n⚠️ Set a status channel with `/setchannel` to see updates"
        await interaction.followup.send(f"✅ Ping service '{name}' added successfully! (ID: {service_id}){channel_warning}", ephemeral=True)
        
    except ValueError as e:
        await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)
    except Exception as e:
        logger.error(f"Error adding ping service: {e}")
        await interaction.followup.send("❌ Error adding ping service to database", ephemeral=True)

@bot.tree.command(name="removeservice", description="Remove a service from monitoring")
async def remove_service_command(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        success = await db_manager.remove_service(name)
        if success:
            if name in service_statuses:
                del service_statuses[name]
            
            results = await check_all_services()
            if status_channel_id:
                await create_or_update_status_message(results)
            await update_bot_status(results)
            
            await interaction.followup.send(f"✅ Service '{name}' removed successfully!", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Service '{name}' not found", ephemeral=True)
            
    except Exception as e:
        logger.error(f"Error removing service: {e}")
        await interaction.followup.send("❌ Error removing service from database", ephemeral=True)

@bot.tree.command(name="listservices", description="List all monitored services")
async def list_services_command(interaction: discord.Interaction):
    try:
        services = await db_manager.get_active_services()
        
        if not services:
            await interaction.response.send_message("📋 No services are currently being monitored.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="📋 Monitored Services",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        for service in services:
            if service['service_type'] == 'http':
                value = f"**Type:** HTTP\n**URL:** {service['url']}\n**Timeout:** {service['timeout_seconds']}s"
            elif service['service_type'] == 'tcp':
                value = f"**Type:** TCP\n**Host:** {service['host']}\n**Port:** {service['port']}\n**Timeout:** {service['timeout_seconds']}s"
            else:
                value = f"**Type:** PING\n**Host:** {service['host']}\n**Timeout:** {service['timeout_seconds']}s"
            
            embed.add_field(
                name=service['name'],
                value=value,
                inline=True
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error listing services: {e}")
        await interaction.response.send_message("❌ Error retrieving services from database", ephemeral=True)

@bot.tree.command(name="setchannel", description="Set the channel for status updates (admin only)")
async def set_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
        return
    
    global status_channel_id, status_message_id
    
    await interaction.response.defer()
    
    try:
        try:
            test_embed = discord.Embed(
                title="✅ Channel Set Successfully",
                description=f"This channel will now receive status updates from {bot.user.mention}",
                color=discord.Color.green()
            )
            test_message = await channel.send(embed=test_embed)
            await test_message.delete()
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ I don't have permission to send messages in {channel.mention}. "
                "Please check my permissions.", 
                ephemeral=True
            )
            return
        except Exception as e:
            await interaction.followup.send(f"❌ Error testing channel: {str(e)}", ephemeral=True)
            return
        
        await db_manager.save_status_channel_id(channel.id)
        status_channel_id = channel.id
        
        status_message_id = None
        
        results = await check_all_services()
        if results:
            await create_or_update_status_message(results)
            success_msg = f"✅ Status channel set to {channel.mention}!\nStatus message created with current service data."
        else:
            embed = discord.Embed(
                title="📊 Service Monitor Ready",
                description="No services configured yet. Use `/addservice` to start monitoring!",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            embed.set_footer(text="Monitoring will begin once services are added")
            
            status_message = await channel.send(embed=embed)
            status_message_id = status_message.id
            await db_manager.save_status_message_id(status_message_id)
            
            success_msg = f"✅ Status channel set to {channel.mention}!\nReady to monitor services."
        
        await interaction.followup.send(success_msg, ephemeral=True)
        logger.info(f"Status channel set to {channel.name} ({channel.id})")
        
    except Exception as e:
        logger.error(f"Error setting status channel: {e}")
        await interaction.followup.send("❌ Error setting status channel", ephemeral=True)

@bot.tree.command(name="channelinfo", description="Show current status channel info")
async def channel_info_command(interaction: discord.Interaction):
    global status_channel_id, status_message_id
    
    embed = discord.Embed(
        title="📺 Channel Configuration",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    if status_channel_id:
        channel = bot.get_channel(status_channel_id)
        if channel:
            embed.add_field(
                name="Status Channel",
                value=f"{channel.mention} (ID: {status_channel_id})",
                inline=False
            )
            
            if status_message_id:
                embed.add_field(
                    name="Status Message",
                    value=f"[Jump to Message](https://discord.com/channels/{interaction.guild.id}/{status_channel_id}/{status_message_id})",
                    inline=False
                )
            else:
                embed.add_field(
                    name="Status Message",
                    value="No active status message",
                    inline=False
                )
        else:
            embed.add_field(
                name="Status Channel",
                value=f"❌ Channel not found (ID: {status_channel_id})",
                inline=False
            )
    else:
        embed.add_field(
            name="Status Channel",
            value="❌ No channel configured\nUse `/setchannel` to set one",
            inline=False
        )
    
    alert_channel_id = ALERT_CHANNEL_ID
    if alert_channel_id:
        alert_channel = bot.get_channel(alert_channel_id)
        embed.add_field(
            name="Alert Channel",
            value=f"{alert_channel.mention} (from .env)" if alert_channel else f"❌ Channel not found (ID: {alert_channel_id})",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="status", description="Force check current status of all services")
async def status_command(interaction: discord.Interaction):
    await interaction.response.defer()
    
    try:
        results = await check_all_services()
        if status_channel_id:
            await create_or_update_status_message(results)
        await update_bot_status(results)
        await interaction.followup.send("✅ Status check completed and message updated!", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await interaction.followup.send("❌ An error occurred while checking services.", ephemeral=True)

@bot.tree.command(name="uptime", description="Show uptime statistics for all services") 
async def uptime_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📊 Service Uptime Statistics",
        timestamp=datetime.now(),
        color=discord.Color.blue()
    )
    
    if not service_statuses:
        embed.description = "No services have been checked yet."
        await interaction.response.send_message(embed=embed)
        return
    
    description = ""
    for name, stats in service_statuses.items():
        status_emoji = "🟢" if stats['status'] == 'up' else "🔴"
        
        description += f"{status_emoji} **{name}**\n"
        
        last_check = stats['last_check']
        last_check_str = last_check.strftime("%Y-%m-%d %H:%M:%S") if last_check else "Never"
        description += f"   └ Last Check: {last_check_str}\n"
        
        if stats['last_response_time']:
            if stats['last_response_time'] < 1:
                description += f"   └ Last Response: {stats['last_response_time']*1000:.0f}ms\n"
            else:
                description += f"   └ Last Response: {stats['last_response_time']:.2f}s\n"
        
        description += "\n"
    
    embed.description = description
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="recreate", description="Create a new status message (admin only)")
async def recreate_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
        return
    
    global status_message_id
    status_message_id = None
    
    await interaction.response.defer()
    
    try:
        results = await check_all_services()
        if status_channel_id:
            await create_or_update_status_message(results)
        await update_bot_status(results)
        await interaction.followup.send("✅ New status message created!", ephemeral=True)
    except Exception as e:
        logger.error(f"Error recreating status message: {e}")
        await interaction.followup.send("❌ Error creating new status message.", ephemeral=True)

@bot.tree.command(name="refreshstatus", description="Force refresh bot status (admin only)")
async def refresh_status_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        results = await check_all_services()
        await update_bot_status(results)
        
        up_count = sum(1 for r in results if r['status'] == 'up')
        down_count = len(results) - up_count
        
        await interaction.followup.send(
            f"✅ Bot status refreshed!\n"
            f"**Services:** {len(results)} total ({up_count} up, {down_count} down)", 
            ephemeral=True
        )
        
    except Exception as e:
        logger.error(f"Error refreshing status: {e}")
        await interaction.followup.send("❌ Error refreshing bot status", ephemeral=True)

@bot.tree.command(name="debug", description="Show debug info (admin only)")
async def debug_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        db_services = await db_manager.get_active_services()
        
        memory_services = len(service_statuses)
        
        current_activity = bot.activity.name if bot.activity else "None"
        current_status = str(bot.status)
        
        embed = discord.Embed(
            title="🔍 Debug Information",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        embed.add_field(
            name="Database Services", 
            value=f"{len(db_services)} active services", 
            inline=True
        )
        
        embed.add_field(
            name="Memory Services", 
            value=f"{memory_services} in memory", 
            inline=True
        )
        
        embed.add_field(
            name="Status Channel", 
            value=f"{'Set' if status_channel_id else 'Not configured'}", 
            inline=True
        )
        
        embed.add_field(
            name="Bot Status", 
            value=f"Status: {current_status}\nActivity: {current_activity}", 
            inline=False
        )
        
        if db_services:
            service_list = "\n".join([f"• {s['name']} ({s['service_type']})" for s in db_services])
            embed.add_field(
                name="Services List",
                value=service_list[:1024],
                inline=False
            )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error in debug command: {e}")
        await interaction.followup.send("❌ Error getting debug info", ephemeral=True)

if __name__ == "__main__":
    bot.run(TOKEN)