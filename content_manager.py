import asyncio
import argparse
import sys
import os
import csv
import uuid
from datetime import datetime

# Add the current directory to Python path to import your modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import Config
from database import Database

class ContentManager:
    def __init__(self):
        self.initialized = False
    
    async def initialize(self):
        """Initialize database connection"""
        try:
            Database.pool = await Database.get_connection()
            await Database.init_db()
            self.initialized = True
            print("✅ Database initialized successfully")
        except Exception as e:
            print(f"❌ Failed to initialize database: {e}")
            raise
    
    async def add_content(self, content_name, google_drive_file_id, file_type="document"):
        """Add a single content to the CMS library"""
        if not self.initialized:
            await self.initialize()
        
        try:
            content_id = str(uuid.uuid4())
            
            await Database.add_content_to_cms_library(
                content_id, content_name, google_drive_file_id, file_type
            )
            
            print(f"✅ Added: {content_name} (ID: {content_id}, Drive ID: {google_drive_file_id})")
            return content_id
            
        except Exception as e:
            print(f"❌ Failed to add {content_name}: {e}")
            return None
    
    async def add_content_bulk(self, content_list):
        """Add multiple contents to the CMS library"""
        if not self.initialized:
            await self.initialize()
        
        success_count = 0
        error_count = 0
        results = []
        
        for content_data in content_list:
            try:
                content_name = content_data['name']
                google_drive_file_id = content_data['drive_id']
                file_type = content_data.get('type', 'document')
                
                content_id = await self.add_content(content_name, google_drive_file_id, file_type)
                
                if content_id:
                    results.append({
                        'name': content_name,
                        'id': content_id,
                        'drive_id': google_drive_file_id,
                        'type': file_type,
                        'status': 'success'
                    })
                    success_count += 1
                else:
                    results.append({
                        'name': content_name,
                        'status': 'error',
                        'error': 'Failed to add content'
                    })
                    error_count += 1
                
            except Exception as e:
                print(f"❌ Failed to add {content_data.get('name', 'unknown')}: {e}")
                results.append({
                    'name': content_data.get('name', 'unknown'),
                    'status': 'error',
                    'error': str(e)
                })
                error_count += 1
        
        return success_count, error_count, results
    
    async def add_content_from_csv(self, csv_file_path):
        """Add contents from CSV file"""
        content_list = []
        
        try:
            with open(csv_file_path, 'r', encoding='utf-8') as file:
                reader = csv.DictReader(file)
                for row in reader:
                    content_list.append({
                        'name': row['name'],
                        'drive_id': row['drive_id'],
                        'type': row.get('type', 'document')
                    })
            
            return await self.add_content_bulk(content_list)
            
        except Exception as e:
            print(f"❌ Error reading CSV file: {e}")
            return 0, 0, []
    
    async def list_contents(self, limit=50, search_term=None):
        """List all contents in the library with optional search"""
        if not self.initialized:
            await self.initialize()
        
        try:
            if search_term:
                query = """
                SELECT content_id, content_name, file_type, google_drive_file_id, uploaded_at
                FROM content_library
                WHERE LOWER(content_name) LIKE LOWER(%s)
                ORDER BY uploaded_at DESC
                LIMIT %s;
                """
                search_param = f"%{search_term}%"
                params = (search_param, limit)
            else:
                query = """
                SELECT content_id, content_name, file_type, google_drive_file_id, uploaded_at
                FROM content_library
                ORDER BY uploaded_at DESC
                LIMIT %s;
                """
                params = (limit,)
            
            async with Database.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query, params)
                    results = await cur.fetchall()
                    
                    if not results:
                        print("📭 No contents found in library")
                        return []
                    
                    contents = []
                    print("\n📚 Content Library:")
                    print("-" * 100)
                    for row in results:
                        content_id, name, file_type, drive_id, uploaded_at = row
                        contents.append({
                            'id': content_id,
                            'name': name,
                            'type': file_type,
                            'drive_id': drive_id,
                            'uploaded_at': uploaded_at
                        })
                        
                        print(f"ID: {content_id}")
                        print(f"Name: {name}")
                        print(f"Type: {file_type}")
                        print(f"Drive ID: {drive_id}")
                        print(f"Uploaded: {uploaded_at}")
                        print("-" * 100)
                    
                    return contents
                        
        except Exception as e:
            print(f"❌ Error listing contents: {e}")
            return []
    
    async def find_best_content_match(self, user_input):
        """Find the best content match for user input using fuzzy matching"""
        if not self.initialized:
            await self.initialize()
        
        try:
            # Get all contents for matching
            all_contents = await self.list_contents(limit=1000)  # Get all contents
            
            if not all_contents:
                print("❌ No contents available for matching")
                return None
            
            # Simple keyword matching (can be enhanced with fuzzywuzzy or similar)
            user_input_lower = user_input.lower()
            best_match = None
            best_score = 0
            
            for content in all_contents:
                content_name_lower = content['name'].lower()
                
                # Calculate match score
                score = 0
                
                # Exact match
                if user_input_lower == content_name_lower:
                    score = 100
                
                # Contains all words
                elif all(word in content_name_lower for word in user_input_lower.split()):
                    score = 80
                
                # Contains some words
                else:
                    matching_words = sum(1 for word in user_input_lower.split() if word in content_name_lower)
                    score = (matching_words / len(user_input_lower.split())) * 60
                
                # Update best match if this is better
                if score > best_score:
                    best_score = score
                    best_match = content
            
            # Only return if we have a reasonably good match
            if best_match and best_score >= 40:
                print(f"✅ Best match for '{user_input}': '{best_match['name']}' (Score: {best_score:.1f})")
                return best_match
            else:
                print(f"❌ No good match found for '{user_input}' (Best score: {best_score:.1f})")
                return None
                
        except Exception as e:
            print(f"❌ Error finding content match: {e}")
            return None
    
    async def cleanup(self):
        """Clean up resources"""
        if hasattr(Database, 'pool') and Database.pool:
            Database.pool.close()
            await Database.pool.wait_closed()

async def main():
    parser = argparse.ArgumentParser(description='Content Management Automation Tool')
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Add content command
    add_parser = subparsers.add_parser('add', help='Add content to library')
    add_parser.add_argument('--name', help='Content name')
    add_parser.add_argument('--drive-id', help='Google Drive file ID')
    add_parser.add_argument('--type', default='document', help='File type (document/video)')
    add_parser.add_argument('--csv', help='CSV file with multiple contents')
    
    # List contents command
    list_parser = subparsers.add_parser('list', help='List contents in library')
    list_parser.add_argument('--limit', type=int, default=50, help='Number of items to show')
    list_parser.add_argument('--search', help='Search term to filter contents')
    
    # Find match command
    match_parser = subparsers.add_parser('match', help='Find best content match for input')
    match_parser.add_argument('input', help='User input to match against content library')
    
    args = parser.parse_args()
    
    manager = ContentManager()
    
    try:
        if args.command == 'add':
            if args.csv:
                success, errors, results = await manager.add_content_from_csv(args.csv)
                print(f"\n📊 Results: {success} successful, {errors} failed")
                
                # Save results to CSV
                output_file = f"content_import_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                with open(output_file, 'w', newline='', encoding='utf-8') as f:
                    fieldnames = ['name', 'id', 'drive_id', 'type', 'status', 'error']
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(results)
                
                print(f"📝 Detailed results saved to {output_file}")
                
            elif args.name and args.drive_id:
                content_id = await manager.add_content(args.name, args.drive_id, args.type)
                if content_id:
                    print(f"✅ Content added with ID: {content_id}")
                else:
                    print("❌ Failed to add content")
            else:
                print("❌ Please provide either --csv file or --name and --drive-id")
        
        elif args.command == 'list':
            await manager.list_contents(args.limit, args.search)
        
        elif args.command == 'match':
            match = await manager.find_best_content_match(args.input)
            if match:
                print(f"🎯 Best match found:")
                print(f"   ID: {match['id']}")
                print(f"   Name: {match['name']}")
                print(f"   Type: {match['type']}")
                print(f"   Drive ID: {match['drive_id']}")
            else:
                print("❌ No suitable match found")
        
        else:
            parser.print_help()
    
    except Exception as e:
        print(f"❌ Error: {e}")
    
    finally:
        await manager.cleanup()

if __name__ == "__main__":
    asyncio.run(main())