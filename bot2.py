from telegram.helpers import escape_markdown
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from config import Config
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, ForceReply, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, PreCheckoutQueryHandler, ContextTypes
from database import Database
import os
import sys
import io
import time
from datetime import datetime, timedelta
import logging
import asyncio
import aiopg
import aiohttp
import socket
import psycopg2
from psycopg2 import errors as pg_errors
import tracemalloc
import random # Import random for jitter
import uuid # For generating unique content IDs
import signal

tracemalloc.start()


# Custom exceptions
class NetworkError(Exception):
    """Custom exception for network-related errors"""
    pass

class TelegramError(Exception):
    """Custom exception for Telegram API errors"""
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class MovieBot:
    def __init__(self):
        self.app = None
        self.initialized = False
        self.max_retries = 5
        self.retry_delay = 10  # seconds
        self._admin_notified = False
        self._bg_tasks = [] #Initialize background tasks lists
        self._shutdown_event = asyncio.Event()
        self.google_drive_service = None # To store the  Google Drive API service client
        self._is_shutting_down = False
        self._user_payment_context = {} # NEW: To store payment_id while waiting for content name

    async def check_network_stability(self):
        """Properly await all async operations with better timeout handling"""
        tests = [
            {
                "name": "DNS Resolution",
                "test": lambda: socket.gethostbyname('api.telegram.org'),
                "error_message": "DNS resolution failed. Check internet connection or DNS settings."
            },
            {
                "name": "HTTP Connectivity",
                "test": self._test_http_connectivity,
                "error_message": "HTTP connectivity failed. Cannot reach external services."
            },
            {
                "name": "Telegram API Connectivity",
                "test": self._test_telegram_connectivity,
                "error_message": "Telegram API connectivity failed. Cannot reach Telegram servers."
            }
        ]

        # Add jitter to delay
        initial_delay = 2 + random.uniform(0, 3)  # 2-5 seconds
        await asyncio.sleep(initial_delay)

        for attempt in range(self.max_retries):
            all_passed = True
            logger.info(f"Running network stability tests (attempt {attempt + 1}/{self.max_retries})")
            
            for test_info in tests:
                try:
                    logger.info(f"Running network test: {test_info['name']}")
                    
                    # Add timeout to each test
                    if asyncio.iscoroutinefunction(test_info["test"]):
                        await asyncio.wait_for(test_info["test"](), timeout=15.0)
                    else:
                        await asyncio.wait_for(
                            asyncio.to_thread(test_info["test"]), 
                            timeout=15.0
                        )
                    
                    logger.info(f"Network test '{test_info['name']}' passed.")
                    
                except asyncio.TimeoutError:
                    logger.error(f"Network test '{test_info['name']}' timed out after 15 seconds.")
                    all_passed = False
                    break
                    
                except Exception as e:
                    logger.error(f"Network test '{test_info['name']}' failed: {e}. {test_info['error_message']}")
                    all_passed = False
                    break

            if all_passed:
                logger.info("All network stability tests passed.")
                self._admin_notified = False  # Reset notification on successful recovery
                return True
            else:
                logger.warning(f"Network stability tests failed. Retrying in {self.retry_delay} seconds (Attempt {attempt + 1}/{self.max_retries})...")
                
                if attempt < self.max_retries - 1:
                    # Exponential backoff with jitter
                    delay = self.retry_delay * (2 ** attempt) + random.uniform(0, 5)
                    await asyncio.sleep(min(delay, 60))  # Cap at 60 seconds
                else:
                    if not self._admin_notified:
                        logger.critical("Maximum network stability retries reached. Persistent network issues.")
                        try:
                            await self._notify_admin("🚨 Critical: Network issues persist. Please check the server.")
                            self._admin_notified = True
                        except Exception as notify_error:
                            logger.error(f"Failed to notify admin about network issues: {notify_error}")
        
        return False



    async def _test_http_connectivity(self):
        async with aiohttp.ClientSession() as session:
            async with session.get("https://www.google.com", timeout=10) as response:
                response.raise_for_status() # Raises HTTPError for bad responses (4xx or 5xx)
        return True


    async def _notify_admin(self, message: str):
        try:
            if self.app and self.app.bot: # Ensure app and bot are initialized
                await self.app.bot.send_message(chat_id=Config.ADMIN_ID, text=message)
                logger.info(f"Admin notified: {message}")
                self._admin_notified = True
        
        except Exception as e:
            logger.error(f"Failed to send admin notification to user {Config.ADMIN_ID}: {e}")

    async def _test_telegram_connectivity(self):
        """Test connectivity to Telegram's API"""
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Test Telegram API endpoint (without authentication)
            async with session.get("https://api.telegram.org") as response:
                # Telegram returns 404 for root, but connection is successful
                if response.status in [200, 404]:
                    return True
                else:
                    response.raise_for_status()
        return True

    async def initialize(self):
        """Initialize the bot components."""
        logger.info("Initializing bot components...")
        try:
            # Database connection pool setup
            Database.pool = await Database.get_connection()
            await Database.init_db()
            logger.info("Database initialized.")
            
            # Google Drive API service client setup
            await self._initialize_google_drive_service()
           
            # Application builder
            self.app = Application.builder().token(Config.TOKEN).build()
            self.initialized = True # Mark as initialized after app is built

            # Handlers
            self.app.add_handler(CommandHandler("start", self.start))
            self.app.add_handler(CommandHandler("request", self.request_content))
            self.app.add_handler(CommandHandler("support", self.handle_support))
            self.app.add_handler(CommandHandler("addcontent", self.handle_add_content)) # Admin command
            self.app.add_handler(CommandHandler("deliver", self.deliver_content_admin)) # Admin command
            self.app.add_handler(CommandHandler("stats", self.get_bot_stats)) # Admin command
            self.app.add_handler(CallbackQueryHandler(self.button_handler))
            self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_message))
            self.app.add_handler(PreCheckoutQueryHandler(self.pre_checkout_callback))
            self.app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, self.successful_payment_callback))
            self.app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, self.handle_content_name_input))
            # --- ADDED HANDLERS FROM JoeMovieBot.py ---
            self.app.add_handler(CommandHandler("mystatus", self.handle_mystatus))
            self.app.add_handler(CommandHandler("admin", self.admin_check))
            self.app.add_handler(CommandHandler("getpayments", self.handle_get_payments))
            self.app.add_handler(CommandHandler("pending", self.handle_pending_payments))
            self.app.add_handler(CallbackQueryHandler(self.handle_retry_request, pattern="retry:.*"))
            self.app.add_handler(CommandHandler("checkpayment", self.handle_check_payment))
            self.app.add_handler(CommandHandler("panel", self.admin_panel))
            self.app.add_handler(CommandHandler("help", self.handle_help))
            self.app.add_handler(CallbackQueryHandler(self.show_help_callback, pattern="show_help"))
            self.app.add_handler(CommandHandler("referral", self.handle_referral))
            self.app.add_handler(CommandHandler("myrewards", self.handle_my_rewards))
            self.app.add_handler(CommandHandler("referralstats", self.handle_referral_stats))
            # --- END ADDED HANDLERS ---

            logger.info("Bot initialization completed successfully.")

        except NetworkError as e:
            logger.critical(f"Network error during bot initialization: {e}")
            await self._notify_admin(f"🚨 Critical: Network error during bot initialization: {e}")
            raise # Re-raise the exception
        
        except TelegramError as e:
            logger.critical(f"Telegram API error during bot initialization: {e}")
            await self._notify_admin(f"🚨 Critical: Telegram API error during bot initialization: {e}")
            raise # Re-raise the exception
        
        except Exception as e:
            logger.critical(f"An unexpected error occurred during bot initialization: {e}")
            await self._notify_admin(f"🚨 Critical: Bot initialization failed unexpectedly: {e}")
            raise # Re-raise the exception

    async def start_background_tasks(self):
        """Start background tasks after the application is running."""
        if not self._is_shutting_down:
            self._bg_tasks.append(asyncio.create_task(self.periodically_cleanup_requests()))
            self._bg_tasks.append(asyncio.create_task(self.periodically_check_membership()))
            logger.info("Background tasks started.")
        
    async def _initialize_google_drive_service(self):
        """Initialize the Google Drive API service client."""
        try:
            scopes = ['https://www.googleapis.com/auth/drive']
            creds = service_account.Credentials.from_service_account_file(
                Config.GOOGLE_DRIVE_CREDENTIALS_PATH, scopes=scopes

            )
            self.google_drive_service = build('drive', 'v3', credentials=creds, cache_discovery=False)
            logger.info("Google Drive API service client initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize Google Drive API service client: {e}")
            await self._notify_admin(f"🚨 Critical: Failed to initialize Google Drive API service client: {e}")
            raise # Re-raise the exception to be caught by the caller
   
    async def cleanup(self):
        """Clean up resources."""
        if self._is_shutting_down:
            return
            
        self._is_shutting_down = True
        logger.info("Starting cleanup...")
        
        # Signal background tasks to stop
        self._shutdown_event.set()
        
        # Cancel and wait for background tasks
        if self._bg_tasks:
            logger.info("Cancelling background tasks...")
            for task in self._bg_tasks:
                if not task.done():
                    task.cancel()
            
            # Wait for tasks to complete cancellation
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            logger.info("Background tasks stopped.")

        # Close database connection pool
        if hasattr(Database, 'pool') and Database.pool:
            try:
                Database.pool.close()
                await Database.pool.wait_closed()
                logger.info("Database connection pool closed.")
            except Exception as e:
                logger.error(f"Error closing database pool: {e}")
        
        logger.info("Cleanup completed.")

    async def is_user_in_channel(self, user_id: int, bot) -> bool:
        """Check if user is a member of the required channel."""
        try:
            member = await bot.get_chat_member(Config.ADVERTISING_CHANNEL_ID, user_id)
            return member.status in ['member', 'administrator', 'creator']
        except Exception as e:
            logger.error(f"Error checking channel membership for user {user_id}: {e}")
            return False

    async def start(self, update, context):
        user_id = update.effective_user.id
        first_name = update.effective_user.first_name
        username = update.effective_user.username
        last_name = update.effective_user.last_name

        await Database.add_or_update_user(user_id, username, first_name, last_name)

        #Add referral handling:
        if context.args and context.args[0].startswith('ref_'):
            referral_code = context.args[0][4:]
            await self._handle_referral_signup(user_id, referral_code)

        keyboard = [
            [InlineKeyboardButton("Request Content", callback_data="request_content")],
            [InlineKeyboardButton("Support", callback_data="support")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        welcome_message = (
            f"Hello {escape_markdown(first_name, version=2)}\\! 👋\n\n"
            "Welcome to the MovieBot\\! I can help you access exclusive cybersecurity content\\.\n\n"
            "Use the buttons below or commands to get started:\n"
            "• /request \\- To request new content\\.\n"
            "• /support \\- For support inquiries\\."
        )
        await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode='MarkdownV2')


    async def request_content(self, update: Update, context):
            # update.effective_user.id is now safe to access because 'update' is an Update object
            user_id = update.effective_user.id

            if not await self.is_user_in_channel(user_id, context.bot):
            # Determine the target for the reply based on the update type
             if update.message:
                await update.message.reply_text(
                    f"🚨 To request content, you must first join our channel: {Config.ADVERTISING_CHANNEL_INVITE_LINK}"
                )
             elif update.callback_query and update.callback_query.message:
                await update.callback_query.message.reply_text(
                    f"🚨 To request content, you must first join our channel: {Config.ADVERTISING_CHANNEL_INVITE_LINK}"
                )
             return

            keyboard = [
            [InlineKeyboardButton("Proceed to Payment", callback_data="proceed_payment")]
        ]
            reply_markup = InlineKeyboardMarkup(keyboard)

        # Determine the target for the reply based on the update type
            if update.message:
               await update.message.reply_text(
                f"To request exclusive content, a payment of {Config.PRICE_AMOUNT / 1:.2f} {Config.CURRENCY} is required.\n\n"
                "Click 'Proceed to Payment' to continue.",
                reply_markup=reply_markup
            )
            elif update.callback_query and update.callback_query.message:
                await update.callback_query.message.reply_text(
                f"To request exclusive content, a payment of {Config.PRICE_AMOUNT / 1:.2f} {Config.CURRENCY} is required.\n\n"
                "Click 'Proceed to Payment' to continue.",
                reply_markup=reply_markup
            )

    async def button_handler(self, update: Update, context):
        query = update.callback_query
        await query.answer() # Acknowledge the callback query

        if query.data == "request_content":
            await self.request_content(update, context) # Pass query object to mimic update
        elif query.data == "proceed_payment":
            await self.send_invoice(query.message.chat.id, context)
        elif query.data == "support":
            await self.handle_support(update, context)
        elif query.data == "show_help": # Added from JoeMovieBot.py
            await self.handle_help(update, context)
        elif query.data.startswith("retry:"): # Added from JoeMovieBot.py
            await self.handle_retry_request(update, context)


    async def send_invoice(self, chat_id: int, context):
        title = "Exclusive Content Access"
        description = f"One-time payment for exclusive cybersecurity content access. Amount: {Config.PRICE_AMOUNT / 1:.2f} {Config.CURRENCY}"
        payload = f"content_access_user_{chat_id}" # Unique payload for this invoice
        provider_token = Config.PAYMENT_PROVIDER_TOKEN
        currency = Config.CURRENCY
        prices = [LabeledPrice("Content Access", Config.PRICE_AMOUNT)]

        try:
            # Create a pending payment record in DB
            payment_id = str(uuid.uuid4()) # Generate a unique payment ID
            await Database.add_pending_payment(
                payment_id=payment_id,
                user_id=chat_id,
                amount=Config.PRICE_AMOUNT,
                currency=Config.CURRENCY
            )

            await context.bot.send_invoice(
                chat_id=chat_id,
                title=title,
                description=description,
                payload=payment_id, # Use our generated payment_id as payload
                provider_token=provider_token,
                currency=currency,
                prices=prices,
                start_parameter="start_param", # Can be any string, required
                need_name=False,
                need_phone_number=False,
                need_email=False,
                need_shipping_address=False,
                is_flexible=False,
                disable_notification=False,
                send_email_to_provider=False,
                send_phone_number_to_provider=False
            )
            logger.info(f"Invoice sent to user {chat_id} with payload {payment_id}. Awaiting payment.")
            # Admin notification about pending request will now be sent AFTER successful payment.

        except Exception as e:
            logger.error(f"Failed to send invoice to {chat_id}: {e}")
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Failed to create payment invoice. Please try again later or contact support."
            )

    async def pre_checkout_callback(self, update, context):
        query = update.pre_checkout_query
        payment_id = query.invoice_payload # Our custom payment_id
        user_id = query.from_user.id

        # Verify the payment ID and ensure it's a valid pending payment
        is_valid_payment = await Database.get_payment_details(payment_id)
        if is_valid_payment and is_valid_payment['status'] == 'pending' and is_valid_payment['user_id'] == user_id:
            await context.bot.answer_pre_checkout_query(query.id, ok=True)
            logger.info(f"Pre-checkout query answered OK for payment {payment_id}")
        else:
            await context.bot.answer_pre_checkout_query(query.id, ok=False, error_message="Invalid or expired payment request.")
            logger.warning(f"Pre-checkout query answered NOT OK for payment {payment_id} from user {user_id}. Details: {is_valid_payment}")

    async def successful_payment_callback(self, update, context):
        # Access payment info directly from update.message.successful_payment
        payment_info = update.message.successful_payment
        user = update.effective_user

        logger.info(f"Successful payment received from user {user.id} for payload: {payment_info.invoice_payload}")

        try:
            # Payment ID is the invoice_payload itself
            payment_id = payment_info.invoice_payload
            # Update payment status in database to 'completed'
            await Database.update_payment_status(payment_id, 'completed', payment_info.provider_payment_charge_id)
            logger.info(f"Payment {payment_id} successfully completed for user {user.id}. Charge ID: {payment_info.provider_payment_charge_id}")

            # NEW: store payment context and prompt user for content name
            self._user_payment_context[user.id] = payment_id
            context.user_data['waiting_for_content_name'] = True

            await update.message.reply_text(
                "🎉 Payment successful! What is the exact name of the exclusive cybersecurity content you'd like to receive? "
                "Please type it below.",
                reply_markup=ForceReply(selective=True, input_field_placeholder="Enter content name")
            )
            logger.info(f"User {user.id} prompted for content name after successful payment {payment_id}")

        except Exception as e:
            logger.error(f"Error processing successful payment {payment_id}: {e}")
            await update.message.reply_text(
                "⚠️ There was an issue processing your payment completion. Please contact support."
            )
        if user.id in self._user_payment_context:
            payment_id = self._user_payment_context[user.id]
            #Give referral reward if applicable
            await self._handle_referral_reward(user.id, payment_info.total_amount)

    # NEW: Handler for user's content name input
    async def handle_content_name_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handles the user's input for desired content name after payment."""
        user = update.effective_user
        content_name = update.message.text.strip()
        logger.info(f"User {user.id} entered content name: '{content_name}'")

        #Clear the waiting_for_content_name flag
        if 'waiting_for_content_name' in context.user_data:
            del context.user_data['waiting_for_content_name']
            
        if user.id not in self._user_payment_context:
            await update.message.reply_text("It looks like you haven't made a recent payment or there was an issue\\. "
                                             "Please use /request_content to start over or contact support\\.")
            return

        payment_id = self._user_payment_context.pop(user.id) # Retrieve and remove context

        try:
            # Lookup content_id by content name in the database
            content_details = await Database.get_content_by_name(content_name)

            if content_details:
                content_id = content_details['content_id']
                await update.message.reply_text(f"Thank you\\! We've noted your request for '{content_name}'\\. "
                                                 "An admin will deliver it shortly\\.")
                logger.info(f"Content ID '{content_id}' found for content name '{content_name}' for payment {payment_id}\\.")

                # Notify admin with payment_id and content_id
                admin_message = (
                    f"🎉 New Content Request for '{escape_markdown(content_name, version=2)}'\\!\n"
                    f"Payment `{escape_markdown(payment_id, version=2)}` completed by user `{escape_markdown(str(user.id), version=2)}` "
                    f"\\(Username: @{escape_markdown(user.username or 'N/A', version=2)}\\)\\.\\n\n"
                    f"Please use `/deliver {escape_markdown(payment_id, version=2)} {escape_markdown(str(content_id), version=2)}` " # FIX: Convert content_id to string here
                    f"to send the content\\."
                )
                await context.bot.send_message(
                    chat_id=Config.ADMIN_ID,
                    text=admin_message,
                    parse_mode="MarkdownV2"
                )
                logger.info(f"Admin notified about content request for payment {payment_id}\\.")

            else:
                await update.message.reply_text(f"Sorry, I couldn't find content with the name '{content_name}'\\. "
                                                 "Please check the name for typos and try again, or contact support "
                                                 "if you believe this is an error\\.")
                logger.warning(f"Content name '{content_name}' not found in database for user {user.id}\\.")
                # Re-add payment context if content not found, allowing user to retry
                self._user_payment_context[user.id] = payment_id
                #Re-set the flag if user needs to retry
                context.user_data['waiting_for_content_name'] = True

        except Exception as e:
            logger.error(f"Error handling content name input from user {user.id}: {e}", exc_info=True)
            await update.message.reply_text("An error occurred while processing your request\\. Please try again later or contact support\\.")
            # Re-add payment context if an error occurred, allowing user to retry
            self._user_payment_context[user.id] = payment_id    
            #Re-set the flag if user needs to retry
            context.user_data['waiting_for_content_name'] = True
          

    async def handle_text_message(self, update, context):
        user_id = update.effective_user.id
        #Check if the bot is waiting for content name from this uesr
        if context.user_data.get('waiting_for_content_name') and user_id in self._user_payment_context:
            await self.handle_content_name_input(update, context)
            return

        # This handler can be used for general chat or future keyword-based interactions
        # For now, it just informs the user to use commands.
        if update.message.chat.type == "private": # Only respond in private chat
            await update.message.reply_text(
                "I'm designed to respond to specific commands and buttons. "
                "Please use /start to see available options or refer to the buttons."
            )
        elif update.message.chat.id == int(Config.ADMIN_CHANNEL_ID):
            logger.info(f"Received message in admin channel: {update.message.text}")
            # Admin channel might receive various messages, log them but don't necessarily respond

    async def periodically_cleanup_requests(self):
        while not self._shutdown_event.is_set():
            try:
                await Database.cleanup_expired_pending_payments()
                logger.info("Expired pending payments cleaned up.")
            except Exception as e:
                logger.error(f"Error during periodic cleanup: {e}")
            finally:
                # Sleep for cleanup interval, but wake up if shutdown is requested
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=Config.CLEANUP_INTERVAL)
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    continue  # Continue the loop

    async def periodically_check_membership(self):
        while not self._shutdown_event.is_set():
            try:
                # This function might be extended to revoke access if user leaves channel after content delivery
                # For now, it only checks at the point of request.
                logger.debug("Running periodic membership check (placeholder for future logic).")
            except Exception as e:
                logger.error(f"Error during periodic membership check: {e}")
            finally:
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=Config.MEMBERSHIP_CHECK_INTERVAL)
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    continue  # Continue the loop

    # --- ADMIN FUNCTIONS (CMS Integration) ---

    async def handle_add_content(self, update, context):
        """
        Admin command to 'register' new content in the bot's database, linking it to the CMS.
        Usage: /addcontent <content_title> <file_path_or_url> [file_type (optional)]
        Example: /addcontent "Advanced Phishing Techniques" "https://yourcms.com/files/phishing.pdf" "document"
        The file_path_or_url should be a direct link to the content in your CMS or cloud storage.
        """
        user_id = update.effective_user.id
        if user_id != Config.ADMIN_ID:
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        args = context.args
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: `/addcontent <content_name> <google_drive_file_id> [file_type]`\n"
                "Example: `/addcontent \"Advanced Phishing Techniques\" \"1aB2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q\" \"document\"`"
            )
            return

        content_title = args[0]
        google_drive_file_id = args[1] 
        file_type = args[2] if len(args) > 2 else "document" # Default to 'document' if not specified

        try:
            # Generate a unique content_id for this new piece of content
            new_content_id = str(uuid.uuid4())
            #Store Google Drive File ID in file_path column
            await Database.add_content_to_cms_library(new_content_id, content_title, google_drive_file_id, file_type)
            await update.message.reply_text(
                f"✅ Content '{content_title}' added to CMS library with ID: `{new_content_id}`.\n"
                f"Google Drive File ID: `{google_drive_file_id}`\n"               
            )
            logger.info(f"Admin {user_id} added content '{content_title}' (ID: {new_content_id}) to CMS library.")
        except Exception as e:
            logger.error(f"Error adding content to CMS library: {e}")
            await update.message.reply_text(f"⚠️ Failed to add content. Error: {e}")

    async def deliver_content_admin(self, update, context):
        """
        Admin command to deliver content to a user after successful payment.
        Usage: /deliver <payment_id> <content_id>
        Example: /deliver a1b2c3d4-e5f6-7890-1234-567890abcdef content_abc-123
        """
        user_id = update.effective_user.id
        if user_id != Config.ADMIN_ID:
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        args = context.args
        if len(args) != 2:
            await update.message.reply_text(
                "Usage: `/deliver <payment_id> <content_id>`\n"
                "Example: `/deliver a1b2c3d4-e5f6-7890-1234-567890abcdef content_abc-123`"
            )
            return

        payment_id = args[0]
        content_id = args[1]

        try:
            payment_details = await Database.get_payment_details(payment_id)
            if not payment_details:
                await update.message.reply_text(f"❌ Payment ID `{payment_id}` not found.")
                return

            if payment_details['status'] != 'completed':
                await update.message.reply_text(
                    f"⚠️ Payment ID `{payment_id}` has status `{payment_details['status']}`. "
                    "Content can only be delivered for 'completed' payments."
                )
                return

            content_info = await Database.get_content_from_cms_library(content_id)
            if not content_info:
                await update.message.reply_text(f"❌ Content ID `{content_id}` not found in CMS library.")
                return

            recipient_user_id = payment_details['user_id']
            google_drive_file_id= content_info['google_drive_file_id']
            file_type = content_info['file_type']

            # Update payment record to link content_id
            await Database.link_content_to_payment(payment_id, content_id)

            # Attempt to deliver the content
            await self._send_content_to_user(recipient_user_id, file_path, file_type, content_info['content_name'])

            await update.message.reply_text(
                f"✅ Content '{content_info['content_name']}' delivered to user `{recipient_user_id}` for payment `{payment_id}`."
            )
            logger.info(f"Admin {user_id} delivered content '{content_id}' to user {recipient_user_id} for payment {payment_id}.")

        except Exception as e:
            logger.error(f"Error delivering content for payment {payment_id}, content {content_id}: {e}")
            await update.message.reply_text(f"⚠️ Failed to deliver content. Error: {e}")

    async def _send_content_to_user(self, user_id: int, google_drive_file_id: str, file_type: str, title: str):
        """
        Downloads content from Google Drive and sends it to the user via Telegram.
        """
        if not self.google_drive_service:
            logger.error("Google Drive service not initialized. Cannot send content.")
            await self.app.bot.send_message(
                chat_id=user_id,
                text="⚠️ Content delivery service not available. Please contact support"
            )
            return

        try:
            
            logger.info(f"Attempting to download file {google_drive_file_id} from Google Drive.")

            # Request the file metadata to get its actual name (optional, but good for saving)
            file_metadata = self.google_drive_service.files().get(fileId=google_drive_file_id, fields='name').execute()
            actual_file_name = file_metadata.get('name', f"{title}.{file_type.lower() if file_type else 'file'}")

            # Download the file content
            request = self.google_drive_service.files().get_media(fileId=google_drive_file_id)
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)
            done = False
            while done is False:
                status, done = await asyncio.to_thread(downloader.next_chunk) # Use asyncio.to_thread for blocking IO
                logger.debug(f"Download progress: {int(status.progress() * 100)}%.")

            file_stream.seek(0) # Rewind the stream to the beginning

            caption = f"Here is your requested content: *{escape_markdown(title, version=2)}*"

            if file_type.lower() == "video":
                await self.app.bot.send_video(
                    chat_id=user_id,
                    video=file_stream,
                    caption=caption,
                    parse_mode='MarkdownV2',
                    filename=actual_file_name # Use the actual file name
                )
            elif file_type.lower() == "document":
                await self.app.bot.send_document(
                    chat_id=user_id,
                    document=file_stream,
                    caption=caption,
                    parse_mode='MarkdownV2',
                    filename=actual_file_name # Use the actual file name
                )
            else:
                # Default to document if type is unknown or not video
                await self.app.bot.send_document(
                    chat_id=user_id,
                    document=file_stream,
                    caption=caption,
                    parse_mode='MarkdownV2',
                    filename=actual_file_name
                )
            logger.info(f"Successfully sent content '{title}' (GD ID: {google_drive_file_id}) to user {user_id}.")

        except Exception as e:
            logger.error(f"Failed to send content (GD ID: {google_drive_file_id}) to user {user_id}: {e}")
            await self.app.bot.send_message(
                chat_id=user_id,
                text="⚠️ An error occurred while delivering your content from Google Drive. Please contact support."
            )
    async def get_bot_stats(self, update, context):
        user_id = update.effective_user.id
        if user_id != Config.ADMIN_ID:
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return

        try:
            stats = await Database.get_stats()
            if stats:
                response = (
                    "📊 *Bot Statistics* 📊\n\n"
                    f"• Total Users: `{stats['total_users']}`\n"
                    f"• Active Users \\(last 30 days\\): `{stats['active_users']}`\n"
                    f"• Total Payments: `{stats['total_payments']}`\n"
                    f"• Pending Payments: `{stats['pending_payments']}`\n"
                    f"• Revenue \\(Completed\\): `{stats['revenue_completed'] / 100:.2f} {Config.CURRENCY}`\n"
                    f"• Revenue \\(Pending\\): `{stats['revenue_pending'] / 100:.2f} {Config.CURRENCY}`\n\n"
                    "For more detailed insights, check the database directly."
                )
            else:
                response = "No statistics available yet."
            await update.message.reply_text(response, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error fetching bot stats: {e}")
            await update.message.reply_text("⚠️ An error occurred while fetching bot statistics.")

    async def handle_support(self, update, context):
        # Determine the target message to reply to
        if hasattr(update, 'message') and update.message:
            target_message = update.message
        elif hasattr(update, 'callback_query') and update.callback_query.message:
            target_message = update.callback_query.message
        else:
            # Fallback or log an error if no message target is found
            logger.error(f"Could not find a message target for handle_support update: {update}")
            return

        await target_message.reply_text(
            "Content requests are non-refundable. "
            "If you have any questions, please visit t.me/cinemate_support for assistance."
        )
        
    # --- NEW HANDLER METHODS FROM JoeMovieBot.py ---

    async def show_help_callback(self, update, context):
        # This is a callback, so it might come from a query.
        # The original JoeMovieBot.py passed update.context, which is unusual.
        # Assuming it should call handle_help with the appropriate message/query object.
        if hasattr(update, 'callback_query'):
            await self.handle_help(update.callback_query, context)
        else:
            await self.handle_help(update, context) # Fallback for direct calls if any

    async def handle_mystatus(self, update, context):
        user_id = update.message.from_user.id
        try:
            requests = await Database.get_user_payments(user_id) # Assuming a method like this exists or can be added to Database
            
            if requests:
                status_msg = "📋 Your Last 5 Requests:\n\n"
                for req in requests:
                    # Assuming req is a dict or tuple with payment_id and request_timestamp
                    payment_id = req.get('payment_id') if isinstance(req, dict) else req[0]
                    request_timestamp = req.get('request_timestamp') if isinstance(req, dict) else req[1]
                    status_msg += f"• `{payment_id}` - {request_timestamp.strftime('%Y-%m-%d %H:%M')}\n"
                status_msg += "\nUse /support if you have questions."
            else:
                status_msg = "You haven't made any requests yet. Use /start to begin!"
            
            await update.message.reply_text(status_msg, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error checking user status for {user_id}: {e}")
            await update.message.reply_text(f"⚠️ Error checking your status: {e}")

    async def admin_check(self, update, context):
        """Verify admin status"""
        if update.message.from_user.id == Config.ADMIN_ID:
            await update.message.reply_text("✅ You are recognized as admin!")
        else:
            await update.message.reply_text("❌ Access denied!")

    async def handle_get_payments(self, update, context):
        """List all payment IDs with statuses"""
        if update.message.from_user.id != Config.ADMIN_ID:
            return await update.message.reply_text("❌ Admin only!")
        
        try:
            payments = await Database.get_all_payment_ids() # Assuming this method exists in Database
            if not payments:
                return await update.message.reply_text("No payments found in the database.")
            
            response = "📋 All Payment IDs:\n\n"
            for payment_id, status in payments:
                status_emoji = "✅" if status == "completed" else "⏳"
                response += f"{status_emoji} `{payment_id}` - {status}\n"
            
            if len(response) > 4000:
                parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
                for part in parts:
                    await update.message.reply_text(part, parse_mode='Markdown')
            else:
                await update.message.reply_text(response, parse_mode='Markdown')
                
        except Exception as e:
            logger.error(f"Error in handle_get_payments: {e}")
            await update.message.reply_text(f"⚠️ Error: {e}")

    async def handle_pending_payments(self, update, context):
        """Show admin all pending payments (without file info)"""
        if update.message.from_user.id != Config.ADMIN_ID:
            return await update.message.reply_text("❌ Admin only!")
        
        try:
            pending_payments = await Database.get_pending_payments_for_admin() # Assuming this method exists in Database
            
            if not pending_payments:
                return await update.message.reply_text("✅ No pending payments - all caught up!")
            
            response = "📋 Pending Payments (need content files):\n\n"
            for payment in pending_payments:
                # Assuming payment is a dict or tuple with payment_id, user_id, amount, currency, request_timestamp
                # and that get_user_info returns a dict or tuple with username
                user_info = await Database.get_user_info(payment['user_id']) if isinstance(payment, dict) else await Database.get_user_info(payment[1])
                username = f"@{user_info['username']}" if user_info and user_info.get('username') else "No username"
                
                payment_id = payment.get('payment_id') if isinstance(payment, dict) else payment[0]
                amount = payment.get('amount') if isinstance(payment, dict) else payment[2]
                currency = payment.get('currency') if isinstance(payment, dict) else payment[3]
                request_timestamp = payment.get('request_timestamp') if isinstance(payment, dict) else payment[4]

                response += (
                    f"🆔 Payment ID: `{payment_id}`\n"
                    f"👤 User: {username} ({payment.get('user_id') if isinstance(payment, dict) else payment[1]})\n"
                    f"💰 Amount: {amount/100} {currency}\n"
                    f"⏰ Requested: {request_timestamp.strftime('%Y-%m-%d %H:%M')}\n"
                    f"🔗 To process: `/deliver {payment_id} <content_id>`\n\n" # Changed to /deliver
                )
            
            if len(response) > 4000:
                parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
                for part in parts:
                    await update.message.reply_text(part, parse_mode='Markdown')
            else:
                await update.message.reply_text(response, parse_mode='Markdown')
                
        except Exception as e:
            logger.error(f"Error in handle_pending_payments: {e}")
            await update.message.reply_text(f"⚠️ Error: {e}")

    async def handle_retry_request(self, update, context):
        # Determine the target message to reply to
        if hasattr(update, 'message') and update.message:
            target_message = update.message
        elif hasattr(update, 'callback_query') and update.callback_query.message:
            target_message = update.callback_query.message
            # For callback queries, remember to answer the query itself
            await update.callback_query.answer(
                "✅ Your payment is valid for a new content request. Please use /request to start a new request.",
                show_alert=True
            )
        else:
            logger.error(f"Could not find a message target for handle_retry_request update: {update}")
            return

        payment_id = update.callback_query.data.split(":")[1]
        user_id = update.callback_query.from_user.id
        
        try:
            # In bot1.py, payments are linked to content delivery, not content requests.
            # Reusing a payment means re-initiating the delivery process for a new content.
            # This logic needs to be adapted. For now, we'll simplify it.
            
            # Check if the payment is valid and can be reused (e.g., not already linked to a delivered content)
            payment_details = await Database.get_payment_details(payment_id)
            if not payment_details or payment_details['status'] != 'completed' or payment_details['content_id'] is not None:
                await update.callback_query.answer(
                    "❌ This payment cannot be reused for a new content request.",
                    show_alert=True
                )
                return

            # Mark the old payment as 'reused' or create a new one linked to the old.
            # For simplicity, we'll just allow them to request new content with this payment ID.
            # The admin will then use /deliver with this payment_id and a new content_id.
            
            await update.callback_query.answer(
                "✅ Your payment is valid for a new content request. Please use /request to start a new request.",
                show_alert=True
            )
            # Optionally, send a message to the user to guide them
            await context.bot.send_message(
                user_id,
                "Your previous payment is valid! Please use the /request command to initiate a new content request."
            )
            
            # Notify admin about the retry attempt
            user_info = await Database.get_user_info(user_id)
            username = f"@{user_info['username']}" if user_info and user_info.get('username') else "No username"
            await self._notify_admin(
                f"🔄 User {username} ({user_id}) attempted to reuse payment `{payment_id}`. "
                f"It's marked as valid for a new content delivery."
            )

        except Exception as e:
            logger.error(f"Error handling retry request for payment {payment_id}: {e}")
            await update.callback_query.answer(
                "⚠️ Error processing your request. Please try again.",
                show_alert=True
            )

    async def handle_check_payment(self, update, context):
        """Check details of a specific payment"""
        if update.message.from_user.id != Config.ADMIN_ID:
            return await update.message.reply_text("❌ Admin only!")
        
        try:
            if len(update.message.text.split()) > 1:
                payment_id = update.message.text.split()[1]
                await self._show_payment_details(update.message, payment_id)
            else:
                msg = await update.message.reply_text("Please enter the Payment ID to check:")
                # Store message_id to filter replies
                context.user_data['check_payment_msg_id'] = msg.message_id
                # Add handler with filters to ensure it only responds to the next message from the same user
                context.dispatcher.add_handler(MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ReplyToMessage(message_id=msg.message_id), 
                    self._process_check_payment_step
                ), group=1) # Use a higher group to prioritize
                
        except Exception as e:
            logger.error(f"Error in handle_check_payment: {e}")
            await update.message.reply_text(f"⚠️ Error: {e}")

    async def _process_check_payment_step(self, update, context):
        """Process payment ID for checking"""
        try:
            payment_id = update.message.text.strip()
            await self._show_payment_details(update.message, payment_id)
            # Remove the temporary handler after use
            if 'check_payment_msg_id' in context.user_data:
                del context.user_data['check_payment_msg_id']
            # This handler is added with a lambda, so it needs to be removed carefully.
            # A more robust way would be to use ConversationHandler or a unique callback_data.
            # For simplicity, we'll assume it's a one-time use.
            # The current implementation of `add_handler` with `group=1` means it's added to a list.
            # Removing it requires iterating through `context.dispatcher.handlers[1]`.
            # This is a common pattern for temporary handlers.
            current_handlers = context.dispatcher.handlers[1][:] # Get a copy
            for handler in current_handlers:
                if hasattr(handler, 'callback') and handler.callback == self._process_check_payment_step:
                    context.dispatcher.remove_handler(handler, group=1)

        except Exception as e:
            logger.error(f"Error in _process_check_payment_step: {e}")
            await update.message.reply_text(f"⚠️ Error: {e}")

    async def _show_payment_details(self, message, payment_id):
        """Show details of a specific payment"""
        try:
            payment = await Database.get_payment_details(payment_id) # Assuming this method exists and returns a dict
            
            if not payment:
                return await message.reply_text("❌ Payment ID not found")
            
            user_info = await Database.get_user_info(payment['user_id'])
            username = f"@{user_info['username']}" if user_info and user_info.get('username') else f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip()
            status_emoji = "✅" if payment['status'] == "completed" else "⏳"
            
            response = (
                f"📋 Payment Details:\n\n"
                f"🆔 Payment ID: `{payment['payment_id']}`\n"
                f"👤 User: {username} ({payment['user_id']})\n"
                f"💰 Amount: {payment['amount']/100} {payment['currency']}\n"
                f"📊 Status: {status_emoji} {payment['status']}\n"
                f"⏰ Requested: {payment['request_timestamp'].strftime('%Y-%m-%d %H:%M')}\n"
            )
            
            if payment['completion_timestamp']:
                response += f"✅ Completed: {payment['completion_timestamp'].strftime('%Y-%m-%d %H:%M')}\n"
            
            if payment['content_id']: # Using 'content_id' from bot1.py's schema
                content_info = await Database.get_content_from_cms_library(payment['content_id'])
                content_title = content_info['title'] if content_info else "Unknown Content"
                response += (
                    f"\n🎬 Content Info:\n"
                    f"📁 Content Title: {content_title}\n"
                    f"🆔 Content ID: `{payment['content_id']}`\n"
                )
            else:
                response += "\n⚠️ No content linked yet\n"
                response += f"🔗 To link: `/deliver {payment['payment_id']} <content_id>`"
            
            await message.reply_text(response, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error in _show_payment_details for {payment_id}: {e}")
            await message.reply_text(f"⚠️ Error: {e}")

    async def admin_panel(self, update, context):
        if update.message.from_user.id == Config.ADMIN_ID:
            keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton("/addcontent"), KeyboardButton("/deliver")],
                    [KeyboardButton("/stats"), KeyboardButton("/pending")],  
                    [KeyboardButton("/checkpayment"), KeyboardButton("/getpayments")]
                ],
                resize_keyboard=True
            )
            await update.message.reply_text(
                "Admin panel:",
                reply_markup=keyboard
            )
        else:
            await update.message.reply_text("❌ Access denied!")
    
    async def _get_user_referral_code(self, user_id):
        """Get or create a referral code for user"""
        try:
            async with Database.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT referral_code FROM referrals WHERE referrer_id = %s",
                        (user_id,)
                    )
                    existing_code = await cur.fetchnone()

                    if existing_code:
                        return existing_code[0]
                    
                    #Generate new code
                    import random
                    import string
                    random_str = ''.join(random.choices(string.ascii_upercase + string.digits, k=6))
                    referral_code = f"{user_id}_{random_str}"

                    await cur.execute(
                        "INSERT INTO referrals(referrer_id, referrer_code) VALUES (%s, %s)",
                        (user_id, referral_code)
                    )
                    return referral_code
        except Exception as e:
            logger.error(f"Error generating referral code: {e}")
            return f"ref_{user_id}"
        
    async def _handle_referral_signup(self, referred_user_id, referral_code):
        """Process new user coming from referral link"""
        try:
            async with Database.pool.acqiure() as conn:
                async with conn.cursor() as cur:
                    #Get referrer who shared this link
                    await cur.execute(
                        "SELECT referrer_id FROM referrals WHERE referral_code = %s AND referred_id IS NULL",
                        (referral_code,)
                    )
                    referrer = await cur.fetchone()
                    
                    if referrer:
                        referrer_id = referrer[0]
                        #Mark referrer as used
                        await cur.execute(
                            "UPDATE referrals SET referred_id = %s, used_at = NOW() WHERE referral_code = %s",
                            (referred_user_id, referrer_code)
                        )
                        
                        #send welcome message to new user about referral
                        try: 
                            await self.app.bot.send_message(
                                chat_id = referred_user_id,
                                text = "Welcome! You joined through a friends referral."
                            )
                        except:
                            pass
                        logger.info(f"Referral recorded: {referrer_id} -> {referred_user_id}")
        except Exception as e:
            logger.error(f"Error processing referral signup: {e}")

    async def _handle_referral_reward(self, referred_user_id, amount_paid):
        """Give Rewards when referred users makes purchase"""
        try:
            async with Database.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    #Find who referred this user
                    await cur.execute(
                        "SELECT referre_id FROM referrals WHERE referrer_id = %s AND reward_given = FALSE",
                        (referred_user_id,)
                    )
                    referreal = await cur.fetchone()
                    
                    if referral:
                        referrer_id = referral[0]
                        reward_amount = amount_paid * 0.2 #20% rewards

                        #Give reward
                        await cur.execute(
                            "INSERT INTO user_rewards (user_id, reward_type, reward_value) VALUES (%s, 'referral', %s)",
                            (referrer_id, reward_amount)
                        )
                        #Mark reward as given
                        await cur.execute(
                            "UPDATE referrals SET reward_given = TRUE WHERE referrred_id = %s",
                            (referred_user_id,)
                        )
                        #Notify referrer 
                        try:
                            await self.app.bot.send_message(
                                chat_id=referrer_id,
                                text=f"🎉**Reward Earned!**\n\n"
                                f"for your friend made their first purchase!\n"
                                f"**Credit earned: ** ${reward_amount/100:.2f}\n\n"
                                f"Use /myrewards to see your balance"
                            )
                        except:
                            pass
        except Exception as e:
            logger.errror(f"Error giving referral reward: {e}")     

    async def handle_referral(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
         """Generate referral link for user"""
         user_id = update.effective_user.id

         referral_code = await self._get_user_referral_code(user_id)
         bot_username = (await self.app.bot.get_me()).username
         referral_link = f"https://t.me/{bot_username}?start=ref_{referral_code}"

         message=(
            "🎉**Share and Earn Rewards!"
            "Invite friends and earn rewards when they join!\n\n"
            f"Your referral link:**\n`{referral_link}`\n\n"
            "**How it works:**\n"
            "•Share your link with friends\n"
            "•When they join and make a purchase\n"
            "•You get 20% of their first payment as credit!\n\n"
            "**current rewards: **\n"
            "•3 referrals = 1 free content\n"
            "•5 referrals = 15% permanent discount! "
         )
         await update.message.reply_text(message, parse_mode='Markdown')

    async def handle_my_rewards(self, Update, context):
        """Show users their available rewards"""
        user_id = Update.effective_user.id
        try:
            async with Database.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(                 
                       "SELECT reward_type, reward_value, earned_at FROM user_rewards"
                        "WHERE user_id = %s ORDER BY earned_at DESC",
                        (user_id,)
                    )
                    rewards = await cur.fetchall()
                   
                    if rewards:
                        message = "💰 ** Your Rewards & Credits** 💰\n\n"
                        total_active = 0
                        total_all = 0

                        for reward in rewards:
                            reward_type = reward[0]
                            reward_value = float(reward[1]) if reward[1] else 0
                            earned_at = reward[2]
                            status = reward[3]


                            total_all += reward_value
                            if status == "active":
                                total_active += reward_value
                            
                            status_icon = "✅" if status == "active" else "⏳" if status == "pending" else "❌"
                            message += f"•{status_icon} {reward_type.title()}: ${reward_value/100:.2f} ({earned_at.strftime('%Y-%m-%d')}) - {status}\n"
                        message += f"\n**Total available: ** ${total_value/100:.2f}\n\n"
                        messgae += "Credits are automatically applied to your next purchase!"

                    else:
                        message = (
                            "You don't have any rewards yet.\n\n"
                            "Use /referral to get your referral link and start earning!\n\n"
                            "Earn 20% of your friends' first purchases!"
                        )
                    await update.message.reply_text(message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error fetching rewards: {e}")
            await Update.message.reply_text("Error fetching your rewards. Please try again later.")   

    async def handle_referral_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin command to see referral statistics - /referralstats command"""
        if update.effective_user.id != Config.ADMIN_ID:
            return await update.message.reply_text("❌ Admin Only")

        try:
            async with Database.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                    SELECT COUNT(*) as total_referrals,
                    COUNT(used_at) as used_referrals,
                    SUM(CASE WHEN reward_given THEN 1 ELSE 0 END) as rewards_given,
                    COUNT(DISTINCT referrer_id) as active_referrers FROM referrals
                    """)
                    stats = await cur.fetchone()

                    message = (
                        "📊 ***Referral Program Statistics*** 📊\n\n"
                        f"•Total referrals: {stats[0]}\n"
                        f"•Successful signups: {stats[1]}\n"
                        f"•Rewards given: {stats[2]}\n"
                        f"•Active referrers: {stats[3]}\n\n"
                        "Use /topreferrers to see leaderboard"                        
                    )
                    await update.message.reply_text(message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error getting referral stats: {e}")
            await update.message.reply_text("Error fetching statistics.")


    async def handle_help(self, update, context):
        user_id = update.effective_user.id
        is_admin = (user_id == Config.ADMIN_ID)
       
        help_text = """
🎬 *Cinemate Bot Commands* 🎬

*Main Commands:*
/start - Start interacting with the bot
/help - Show this help message
/support - Contact support

*Content Commands:*
/request - Request a content (also available via button)
/mystatus - Check your pending requests

*Earn Rewards:*
•Use /referral to get your unique link
•Share with friends and earn 20% of their first purchase
•Check /myrewards to see your earnings
"""
        if is_admin:
            help_text += """      

*Admin Commands* (Admin only):
/addcontent - Add a content to the CMS library
/deliver - Deliver content to a user
/checkpayment - Check payment details
/pending - List pending payments
/stats - View bot statistics
/panel - Admin control panel
/getpayments - List all payment IDs
/referralstats - View referral program stats
"""
        help_text += "\n Click buttons or type commands to interact with me"
        await update.message.reply_text(help_text, parse_mode='Markdown')

    # --- END NEW HANDLER METHODS ---

async def main():
    bot = MovieBot()
    
    # Setup signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating shutdown...")
        # Create a task to handle shutdown
        asyncio.create_task(shutdown_handler())
    
    async def shutdown_handler():
        logger.info("Shutdown handler called, stopping bot...")
        if bot.app and bot.app.running:
            await bot.app.stop()
        await bot.cleanup()
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    max_startup_retries = 5
    startup_retry_delay = 30  # seconds
    
    for attempt in range(max_startup_retries):
        try:
            logger.info(f"Starting bot initialization attempt {attempt + 1}/{max_startup_retries}")
            
            # Check network stability first
            network_ok = await bot.check_network_stability()
            if not network_ok:
                logger.error("Network stability check failed, skipping this attempt")
                if attempt < max_startup_retries - 1:
                    await asyncio.sleep(startup_retry_delay)
                    continue
                else:
                    raise NetworkError("Network stability check failed after all retries")
            
            # Initialize bot components
            await bot.initialize()
            
            # Initialize and start the application with retry logic
            logger.info("Initializing Telegram application...")
            initialization_successful = False
            
            for init_attempt in range(3):  # 3 attempts for app initialization
                try:
                    await bot.app.initialize()
                    initialization_successful = True
                    logger.info("Telegram application initialization successful")
                    break
                except Exception as init_error:
                    logger.warning(f"App initialization attempt {init_attempt + 1}/3 failed: {init_error}")
                    if init_attempt < 2:  # Don't sleep after the last attempt
                        await asyncio.sleep(10)
            
            if not initialization_successful:
                raise TelegramError("Failed to initialize Telegram application after 3 attempts")
            
            # Start the application
            logger.info("Starting Telegram application...")
            await bot.app.start()
            
            # Start background tasks after the app is running
            await bot.start_background_tasks()
            
            logger.info("Bot started successfully, beginning to poll for updates...")
            
            # Start polling with error handling
            try:
                await bot.app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,  # Skip old updates
                    timeout=30,  # 30 second timeout for getting updates
                    bootstrap_retries=3,  # Retry connection 3 times
                )
                
                # Keep the bot running
                await asyncio.Event().wait()
                
            except Exception as polling_error:
                logger.error(f"Error during polling: {polling_error}")
                raise
                
            # If we reach here, the bot started successfully
            break
            
        except (TelegramError, NetworkError) as e:
            logger.error(f"Bot startup attempt {attempt + 1} failed: {e}")
            if attempt < max_startup_retries - 1:
                logger.info(f"Retrying in {startup_retry_delay} seconds...")
                await asyncio.sleep(startup_retry_delay)
            else:
                logger.critical("All startup attempts failed")
                raise
                
        except KeyboardInterrupt:
            logger.info("Bot interrupted by user during startup")
            break
            
        except Exception as e:
            logger.error(f"Unexpected error during startup attempt {attempt + 1}: {e}", exc_info=True)
            if attempt < max_startup_retries - 1:
                logger.info(f"Retrying in {startup_retry_delay} seconds...")
                await asyncio.sleep(startup_retry_delay)
            else:
                logger.critical("All startup attempts failed due to unexpected errors")
                raise
    
    # Handle graceful shutdown
    try:
        logger.info("Bot is running. Press Ctrl+C to stop.")
        # This will block until interrupted
        await asyncio.Event().wait()
        
    except KeyboardInterrupt:
        logger.info("Bot interrupted by user")
    except Exception as e:
        logger.critical(f"Fatal error in bot: {e}", exc_info=True)
    finally:
        logger.info("Starting shutdown sequence...")
        try:
            # Stop the updater first
            if bot.app and hasattr(bot.app, 'updater') and bot.app.updater and bot.app.updater.running:
                logger.info("Stopping updater...")
                await bot.app.updater.stop()
                logger.info("Updater stopped")
            
            # Stop the application
            if bot.app and bot.app.running:
                logger.info("Stopping application...")
                await bot.app.stop()
                logger.info("Application stopped")
            
            # Clean up resources
            await bot.cleanup()
            logger.info("Bot shutdown completed")
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}", exc_info=True)

# Add a function to test network connectivity before starting
async def test_connectivity():
    """Test basic network connectivity before starting the bot"""
    logger.info("Testing network connectivity...")
    
    try:
        # Test DNS resolution
        socket.gethostbyname('api.telegram.org')
        logger.info("DNS resolution test passed")
        
        # Test HTTP connectivity
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get("https://api.telegram.org") as response:
                logger.info(f"HTTP connectivity test passed (status: {response.status})")
        
        return True
        
    except Exception as e:
        logger.error(f"Network connectivity test failed: {e}")
        return False

# Run the bot
if __name__ == "__main__":
    try:
        # Test connectivity first
        if asyncio.run(test_connectivity()):
            logger.info("Network connectivity tests passed, starting bot...")
            asyncio.run(main())
        else:
            logger.critical("Network connectivity tests failed. Please check your internet connection.")
            sys.exit(1)
            
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}", exc_info=True)
        sys.exit(1)

