🎬 Cinemate Bot - Premium Content Delivery System

A sophisticated Telegram bot designed for automated content delivery with integrated payment processing, referral system, and comprehensive admin management.

🌟 Key Features

🤖 Core Functionality

· Automated Content Delivery - Seamlessly deliver digital content after payment
· Payment Integration - Secure Telegram Payments with multiple providers
· Google Drive Integration - Direct content delivery from cloud storage
· Smart Content Matching - AI-powered content discovery based on user input
· Referral System - Earn rewards for bringing new users
· Admin Dashboard - Complete management interface

💰 Payment & Monetization

· One-time payments for content access
· Support for multiple currencies
· Secure payment processing via Telegram
· Automated payment verification
· Revenue tracking and analytics

🔗 Referral Program

· Unique referral links for each user
· 20% commission on referrals' first purchases
· Reward tracking and management
· Multi-tier achievement system

🛠 Admin Features

· Content library management
· Bulk content import via CSV
· Automated delivery system
· Payment monitoring and analytics
· User management
· Referral program statistics

📋 Prerequisites

Required Accounts & Services

1. Telegram Bot Token - From @BotFather
2. PostgreSQL Database - For data storage
3. Google Drive API - For content storage
4. Payment Provider - Stripe or other Telegram-supported providers

Environment Setup

```bash
# Clone the repository
git clone Telegram-bot-project
cd Telegram-bot-project 

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
```

⚙️ Configuration

Environment Variables (.env)

```env
# Telegram Configuration
TOKEN=your_bot_token
ADMIN_ID=your_admin_user_id
ADMIN_CHANNEL_ID=your_admin_channel_id
ADVERTISING_CHANNEL=your_channel_username
ADVERTISING_CHANNEL_INVITE_LINK=your_channel_invite_link

# Database Configuration
DB_NAME=cinemate_bot
DB_USER=postgres
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=5432

# Payment Configuration
PAYMENT_PROVIDER_TOKEN=your_payment_token
CURRENCY=USD
PRICE_AMOUNT=100  # in cents

# System Configuration
REQUEST_EXPIRY_HOURS=24
MEMBERSHIP_CHECK_INTERVAL=86400
CLEANUP_INTERVAL=3600

# Google Drive Configuration
GOOGLE_DRIVE_CREDENTIALS_PATH=credentials.json
GOOGLE_DRIVE_CONTENT_FOLDER_ID=your_folder_id
```

🚀 Installation & Setup

1. Database Setup

```sql
-- The bot automatically creates all necessary tables
-- Just ensure PostgreSQL is running and accessible
```

2. Google Drive Setup

1. Enable Google Drive API in Google Cloud Console
2. Create service account credentials
3. Download credentials as credentials.json
4. Share your content folder with the service account email

3. Bot Deployment

```bash
# Run the main bot
python bot2.py

# For automated content delivery
python continuous_delivery.py

# For content management
python content_manager.py add --name "Content Name" --drive-id "drive_file_id"
```

📁 Project Structure

```
cinemate-bot/
├── bot2.py                 # Main bot application
├── config.py              # Configuration management
├── database.py            # Database operations
├── content_manager.py     # Content library management
├── delivery_automater.py  # Automated delivery system
├── continuous_delivery.py # Continuous delivery service
├── requirements.txt       # Python dependencies
└── README.md             # This file
```

🎯 Usage

For Users

· /start - Begin interaction with the bot
· /request - Request new content (payment required)
· /mystatus - Check your request status
· /referral - Get your referral link
· /myrewards - View earned rewards
· /support - Contact support

For Admins

· /addcontent - Add content to library
· /deliver - Manually deliver content
· /stats - View bot statistics
· /pending - List pending payments
· /panel - Admin control panel
· /referralstats - Referral program analytics

🔧 Management Tools

Content Manager

```bash
# Add single content
python content_manager.py add --name "Movie Title" --drive-id "file_id"

# Bulk import from CSV
python content_manager.py add --csv contents.csv

# List all content
python content_manager.py list

# Find content matches
python content_manager.py match "user search query"
```

Automated Delivery

  ```bash
# Run continuous delivery service
python continuous_delivery.py --interval 300

# Process pending deliveries once
python delivery_automater.py process --strategy recent
```

💾 Database Schema

The system uses PostgreSQL with the following main tables:

· users - User information and activity
· payments - Payment records and status
· content_library - Content metadata and Google Drive links
· referrals - Referral tracking
· user_rewards - Reward system management

🔒 Security Features

· Payment verification and validation
· Content access control
· User authentication
· Secure Google Drive API integration
· SQL injection prevention
· Input sanitization

📊 Analytics & Monitoring

· User activity tracking
· Revenue analytics
· Content popularity metrics
· Referral program performance
· System health monitoring

🤝 Support

For technical support or questions:

· Contact: @cinemate_support (Telegram)
· Issues: GitHub Issues page
· Documentation: Inline code comments



