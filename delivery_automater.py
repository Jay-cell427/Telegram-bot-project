import asyncio
import argparse
import sys
import os
import csv
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import Config
from database import Database
from telegram import Bot
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
from content_manager import ContentManager

class DeliveryAutomator:
    def __init__(self):
        self.initialized = False
        self.bot = None
        self.google_drive_service = None
        self.content_manager = ContentManager()
    
    async def initialize(self):
        """Initialize all components"""
        try:
            # Initialize database
            Database.pool = await Database.get_connection()
            await Database.init_db()
            
            # Initialize Telegram bot
            self.bot = Bot(token=Config.TOKEN)
            
            # Initialize Google Drive service
            scopes = ['https://www.googleapis.com/auth/drive']
            creds = service_account.Credentials.from_service_account_file(
                Config.GOOGLE_DRIVE_CREDENTIALS_PATH, scopes=scopes
            )
            self.google_drive_service = build('drive', 'v3', credentials=creds, cache_discovery=False)
            
            # Initialize content manager
            await self.content_manager.initialize()
            
            self.initialized = True
            print("✅ All components initialized successfully")
            
        except Exception as e:
            print(f"❌ Failed to initialize: {e}")
            raise
    
    async def get_pending_deliveries(self):
        """Get all pending deliveries"""
        if not self.initialized:
            await self.initialize()
        
        try:
            return await Database.get_pending_payments_for_admin()
        except Exception as e:
            print(f"❌ Error getting pending deliveries: {e}")
            return []
    
    async def process_delivery(self, payment_id, content_id=None, content_name=None):
        """Process a single delivery with optional content matching"""
        if not self.initialized:
            await self.initialize()
        
        try:
            # Get payment details
            payment = await Database.get_payment_details(payment_id)
            if not payment:
                print(f"❌ Payment {payment_id} not found")
                return False
            
            if payment['status'] != 'completed':
                print(f"❌ Payment {payment_id} status is {payment['status']}, not completed")
                return False
            
            # Get user info for context
            user_info = await Database.get_user_info(payment['user_id'])
            username = f"@{user_info['username']}" if user_info and user_info.get('username') else f"User {payment['user_id']}"
            
            print(f"👤 Processing delivery for {username} (Payment: {payment_id})")
            
            # Determine which content to deliver
            content = None
            
            if content_id:
                # Use specified content ID
                content = await Database.get_content_from_cms_library(content_id)
                if not content:
                    print(f"❌ Content {content_id} not found")
                    return False
            elif content_name:
                # Find content by name using matching logic
                content_match = await self.content_manager.find_best_content_match(content_name)
                if content_match:
                    content = await Database.get_content_from_cms_library(content_match['id'])
                else:
                    print(f"❌ No content found matching '{content_name}'")
                    return False
            else:
                # Try to find content based on user's previous requests or other logic
                # This is where you could implement more sophisticated matching
                print("❌ Either content_id or content_name must be provided")
                return False
            
            user_id = payment['user_id']
            drive_id = content['google_drive_file_id']
            file_type = content['file_type']
            title = content['content_name']
            
            # Send content to user
            await self._send_content_to_user(user_id, drive_id, file_type, title)
            
            # Update payment record
            await Database.link_content_to_payment(payment_id, content['content_id'])
            
            print(f"✅ Successfully delivered '{title}' to {username}")
            
            # Send confirmation to admin
            try:
                await self.bot.send_message(
                    chat_id=Config.ADMIN_ID,
                    text=f"✅ Automatically delivered '{title}' to {username} (Payment: {payment_id})"
                )
            except Exception as e:
                print(f"⚠️ Could not send admin notification: {e}")
            
            return True
            
        except Exception as e:
            print(f"❌ Error processing delivery {payment_id}: {e}")
            return False
    
    async def process_all_pending(self, matching_strategy="keyword"):
        """Process all pending deliveries automatically with content matching"""
        if not self.initialized:
            await self.initialize()
        
        pending_deliveries = await self.get_pending_deliveries()
        
        if not pending_deliveries:
            print("✅ No pending deliveries found")
            return
        
        print(f"📋 Found {len(pending_deliveries)} pending deliveries")
        
        success_count = 0
        error_count = 0
        results = []
        
        for delivery in pending_deliveries:
            payment_id = delivery['payment_id']
            user_id = delivery['user_id']
            
            print(f"\n🔄 Processing payment {payment_id} for user {user_id}")
            
            # Try to determine what content to deliver based on strategy
            content_to_deliver = await self._determine_content_to_deliver(
                user_id, payment_id, matching_strategy
            )
            
            if content_to_deliver:
                success = await self.process_delivery(
                    payment_id, 
                    content_id=content_to_deliver['id']
                )
                
                if success:
                    success_count += 1
                    results.append({
                        'payment_id': payment_id,
                        'user_id': user_id,
                        'content_id': content_to_deliver['id'],
                        'content_name': content_to_deliver['name'],
                        'status': 'success'
                    })
                else:
                    error_count += 1
                    results.append({
                        'payment_id': payment_id,
                        'user_id': user_id,
                        'status': 'error',
                        'error': 'Delivery failed'
                    })
            else:
                error_count += 1
                results.append({
                    'payment_id': payment_id,
                    'user_id': user_id,
                    'status': 'error',
                    'error': 'Could not determine content to deliver'
                })
                print(f"❌ Could not determine content to deliver for payment {payment_id}")
        
        print(f"\n📊 Processed: {success_count} successful, {error_count} failed")
        
        # Save results to CSV
        if results:
            output_file = f"delivery_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                fieldnames = ['payment_id', 'user_id', 'content_id', 'content_name', 'status', 'error']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(results)
            
            print(f"📝 Detailed results saved to {output_file}")
    
    async def _determine_content_to_deliver(self, user_id, payment_id, strategy="keyword"):
        """Determine which content to deliver based on the selected strategy"""
        # In a real implementation, you might:
        # 1. Look at the user's message history to see what they requested
        # 2. Use the most popular content
        # 3. Use content that hasn't been delivered recently
        # 4. Use a round-robin approach
        
        # For now, we'll use a simple strategy: deliver the most recently added content
        try:
            # Get all available contents
            contents = await self.content_manager.list_contents(limit=1000)
            
            if contents:
                # Return the most recently added content
                return contents[0]
            else:
                return None
                
        except Exception as e:
            print(f"❌ Error determining content to deliver: {e}")
            return None
    
    async def _send_content_to_user(self, user_id, google_drive_file_id, file_type, title):
        """Send content to user (copied from your bot code)"""
        try:
            # Download file from Google Drive
            file_metadata = self.google_drive_service.files().get(
                fileId=google_drive_file_id, fields='name'
            ).execute()
            actual_file_name = file_metadata.get('name', f"{title}.{file_type.lower()}")
            
            request = self.google_drive_service.files().get_media(fileId=google_drive_file_id)
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)
            done = False
            
            while not done:
                status, done = downloader.next_chunk()
                print(f"📥 Download progress: {int(status.progress() * 100)}%")
            
            file_stream.seek(0)
            
            # Send to user
            caption = f"Here is your requested content: *{title}*"
            
            if file_type.lower() == "video":
                await self.bot.send_video(
                    chat_id=user_id,
                    video=file_stream,
                    caption=caption,
                    parse_mode='Markdown',
                    filename=actual_file_name
                )
            else:
                await self.bot.send_document(
                    chat_id=user_id,
                    document=file_stream,
                    caption=caption,
                    parse_mode='Markdown',
                    filename=actual_file_name
                )
            
            print(f"📤 Sent {title} to user {user_id}")
            
        except Exception as e:
            print(f"❌ Error sending content to user {user_id}: {e}")
            raise
    
    async def list_pending(self):
        """List all pending deliveries"""
        pending = await self.get_pending_deliveries()
        
        if not pending:
            print("✅ No pending deliveries")
            return []
        
        print(f"\n📋 Pending Deliveries ({len(pending)}):")
        print("=" * 100)
        
        detailed_pending = []
        
        for delivery in pending:
            user_info = await Database.get_user_info(delivery['user_id'])
            username = f"@{user_info['username']}" if user_info and user_info.get('username') else "No username"
            
            detailed_pending.append({
                'payment_id': delivery['payment_id'],
                'user_id': delivery['user_id'],
                'username': username,
                'amount': delivery['amount'],
                'currency': delivery['currency'],
                'request_timestamp': delivery['request_timestamp']
            })
            
            print(f"Payment ID: {delivery['payment_id']}")
            print(f"User: {username} ({delivery['user_id']})")
            print(f"Amount: {delivery['amount']/100} {delivery['currency']}")
            print(f"Requested: {delivery['request_timestamp']}")
            print("-" * 100)
        
        return detailed_pending
    
    async def cleanup(self):
        """Clean up resources"""
        if hasattr(Database, 'pool') and Database.pool:
            Database.pool.close()
            await Database.pool.wait_closed()
        
        if self.google_drive_service:
            self.google_drive_service.close()
        
        await self.content_manager.cleanup()

async def main():
    parser = argparse.ArgumentParser(description='Delivery Automation Tool with Content Matching')
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Single delivery command
    deliver_parser = subparsers.add_parser('deliver', help='Deliver content to user')
    deliver_parser.add_argument('--payment', required=True, help='Payment ID')
    deliver_parser.add_argument('--content-id', help='Content ID to deliver')
    deliver_parser.add_argument('--content-name', help='Content name to match and deliver')
    
    # List pending command
    list_parser = subparsers.add_parser('list', help='List pending deliveries')
    
    # Process all command
    process_parser = subparsers.add_parser('process', help='Process all pending deliveries')
    process_parser.add_argument('--strategy', default='keyword', 
                               choices=['keyword', 'recent', 'popular', 'roundrobin'],
                               help='Content matching strategy')
    
    args = parser.parse_args()
    
    automator = DeliveryAutomator()
    
    try:
        if args.command == 'deliver':
            await automator.initialize()
            
            if not args.content_id and not args.content_name:
                print("❌ Either --content-id or --content-name must be provided")
                return
            
            success = await automator.process_delivery(
                args.payment, 
                args.content_id, 
                args.content_name
            )
            
            if success:
                print("✅ Delivery completed successfully")
            else:
                print("❌ Delivery failed")
        
        elif args.command == 'list':
            await automator.initialize()
            await automator.list_pending()
        
        elif args.command == 'process':
            await automator.initialize()
            await automator.process_all_pending(args.strategy)
        
        else:
            parser.print_help()
    
    except Exception as e:
        print(f"❌ Error: {e}")
    
    finally:
        await automator.cleanup()

if __name__ == "__main__":
    asyncio.run(main())