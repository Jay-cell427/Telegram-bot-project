import asyncio
import sys
import os
import time
import signal
import logging
from datetime import datetime, timedelta

# Add the current directory to Python path to import your modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import Config
from delivery_automator import DeliveryAutomator

class ContinuousDelivery:
    def __init__(self, check_interval=300):  # Default: 5 minutes
        self.check_interval = check_interval
        self.automator = DeliveryAutomator()
        self.running = False
        self.last_check = None
        self.processed_count = 0
        self.error_count = 0
        
        # Set up logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('continuous_delivery.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
    
    async def initialize(self):
        """Initialize all components"""
        try:
            await self.automator.initialize()
            self.logger.info("Continuous delivery system initialized")
            return True
        except Exception as e:
            self.logger.error(f"Failed to initialize: {e}")
            return False
    
    async def check_and_process_deliveries(self):
        """Check for pending deliveries and process them"""
        try:
            self.last_check = datetime.now()
            self.logger.info("Checking for pending deliveries...")
            
            # Get pending deliveries
            pending_deliveries = await self.automator.get_pending_deliveries()
            
            if not pending_deliveries:
                self.logger.info("No pending deliveries found")
                return 0, 0
            
            self.logger.info(f"Found {len(pending_deliveries)} pending deliveries")
            
            # Process deliveries
            success_count = 0
            error_count = 0
            
            for delivery in pending_deliveries:
                payment_id = delivery['payment_id']
                user_id = delivery['user_id']
                
                self.logger.info(f"Processing payment {payment_id} for user {user_id}")
                
                # Determine which content to deliver (using recent strategy)
                content_to_deliver = await self.automator._determine_content_to_deliver(
                    user_id, payment_id, "recent"
                )
                
                if content_to_deliver:
                    success = await self.automator.process_delivery(
                        payment_id, 
                        content_id=content_to_deliver['id']
                    )
                    
                    if success:
                        success_count += 1
                        self.processed_count += 1
                        self.logger.info(f"Successfully delivered content for payment {payment_id}")
                    else:
                        error_count += 1
                        self.error_count += 1
                        self.logger.error(f"Failed to deliver content for payment {payment_id}")
                else:
                    error_count += 1
                    self.error_count += 1
                    self.logger.error(f"Could not determine content to deliver for payment {payment_id}")
            
            self.logger.info(f"Processed: {success_count} successful, {error_count} failed")
            return success_count, error_count
            
        except Exception as e:
            self.logger.error(f"Error in check_and_process_deliveries: {e}")
            return 0, 0
    
    async def run_continuous(self):
        """Run the continuous delivery system"""
        self.running = True
        self.logger.info("Starting continuous delivery system...")
        
        # Initialize
        if not await self.initialize():
            self.logger.error("Failed to initialize, exiting")
            return
        
        # Set up signal handlers for graceful shutdown
        def signal_handler(signum, frame):
            self.logger.info(f"Received signal {signum}, shutting down...")
            self.running = False
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Main loop
        while self.running:
            try:
                # Check and process deliveries
                success, errors = await self.check_and_process_deliveries()
                
                # Log summary every hour
                if datetime.now().minute == 0:  # On the hour
                    self.logger.info(
                        f"Summary - Total processed: {self.processed_count}, "
                        f"Total errors: {self.error_count}, "
                        f"Last check: {self.last_check}"
                    )
                
                # Wait for the next check interval
                self.logger.info(f"Next check in {self.check_interval} seconds...")
                await asyncio.sleep(self.check_interval)
                
            except asyncio.CancelledError:
                self.logger.info("Task cancelled, shutting down")
                break
            except Exception as e:
                self.logger.error(f"Unexpected error in main loop: {e}")
                # Wait a bit before retrying to avoid rapid error loops
                await asyncio.sleep(60)
        
        # Cleanup
        await self.automator.cleanup()
        self.logger.info("Continuous delivery system stopped")

    async def run_with_exponential_backoff(self):
        """Run with exponential backoff on errors"""
        self.running = True
        self.logger.info("Starting continuous delivery system with exponential backoff...")
        
        # Initialize
        if not await self.initialize():
            self.logger.error("Failed to initialize, exiting")
            return
        
        retry_count = 0
        max_retry_interval = 3600  # 1 hour maximum wait
        
        # Set up signal handlers for graceful shutdown
        def signal_handler(signum, frame):
            self.logger.info(f"Received signal {signum}, shutting down...")
            self.running = False
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Main loop
        while self.running:
            try:
                # Reset retry count on successful check
                retry_count = 0
                
                # Check and process deliveries
                success, errors = await self.check_and_process_deliveries()
                
                # Use normal interval if no errors
                wait_time = self.check_interval
                
                # Wait for the next check interval
                self.logger.info(f"Next check in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
                
            except Exception as e:
                retry_count += 1
                self.logger.error(f"Error in main loop (retry {retry_count}): {e}")
                
                # Calculate exponential backoff with jitter
                backoff_time = min(60 * (2 ** retry_count), max_retry_interval)
                jitter = backoff_time * 0.1  # 10% jitter
                wait_time = backoff_time + (random.random() * jitter * 2 - jitter)
                
                self.logger.info(f"Waiting {wait_time:.0f} seconds before retry...")
                await asyncio.sleep(wait_time)
        
        # Cleanup
        await self.automator.cleanup()
        self.logger.info("Continuous delivery system stopped")

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Continuous Delivery Automation System')
    parser.add_argument('--interval', type=int, default=300, 
                       help='Check interval in seconds (default: 300 = 5 minutes)')
    parser.add_argument('--backoff', action='store_true',
                       help='Use exponential backoff on errors')
    
    args = parser.parse_args()
    
    delivery_system = ContinuousDelivery(check_interval=args.interval)
    
    try:
        if args.backoff:
            asyncio.run(delivery_system.run_with_exponential_backoff())
        else:
            asyncio.run(delivery_system.run_continuous())
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
    except Exception as e:
        print(f"Unexpected error: {e}")

if __name__ == "__main__":
    main()