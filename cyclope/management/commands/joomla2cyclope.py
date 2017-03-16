from django.core.management.base import BaseCommand, CommandError
from optparse import make_option
import mysql.connector
import re
from cyclope.models import SiteSettings
from cyclope.apps.articles.models import Article
from cyclope.core.collections.models import Collection, Category, Categorization
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError
import operator
from autoslug.settings import slugify
from datetime import datetime
from django.contrib.auth.models import User
from lxml import html
from lxml.cssselect import CSSSelector # FIXME REQUIRES cssselect
import json
from django.db import transaction

class Command(BaseCommand):
    help = """
    Migrates a site in Joomla to CyclopeCMS.

    Usage: (cyclope_workenv)$~ python manage.py joomla2cyclope --server localhost --database REDECO_JOOMLA --user root --password NEW_PASSWORD --prefix wiphala_

    Required params are server host name, database name and database user and password.
    Optional params are joomla's table prefix.
    """
    # TODO mysql.connector.errors.InterfaceError: 2013: Lost connection to MySQL server during query

    #NOTE django > 1.8 uses argparse instead of optparse module, 
    #so "You are encouraged to exclusively use **options for new commands."
    #https://docs.djangoproject.com/en/1.9/howto/custom-management-commands/
    option_list = BaseCommand.option_list + (
        make_option('--server',
            action='store',
            dest='server',
            default=None,
            help='Joomla host name'
        ),
        make_option('--database',
            action='store',
            dest='db',
            default=None,
            help='Database name'
        ),
        make_option('--user',
            action='store',
            dest='user',
            default=None,
            help='Database user'
        ),
        make_option('--password',
            action='store',
            dest='password',
            default=None,
            help='Database password'
        ),
        make_option('--prefix',
            action='store',
            dest='prefix',
            default='',
            help='Joomla\'s tables prefix'
        ),
        make_option('--default_password',
            action='store',
            dest='joomla_password',
            default=None,
            help='Default password for ALL users. Optional, otherwise usernames will be used.'
        ),
        make_option('--devel',
            action='store_true',
            dest='devel',
            help='Use http://localhost:8000 as site url (development)'
        ),
    )
    
    # class constants
    table_prefix = None
    joomla_password = None
    devel_url = False
    
    def handle(self, *args, **options):
        """Joomla to Cyclope database migration logic"""
        
        self.table_prefix = options['prefix']
        self.joomla_password = options['joomla_password']
        self.devel_url = options['devel']

        # MySQL connection
        cnx = self._mysql_connection(options['server'], options['db'], options['user'], options['password'])
        print "connected to Joomla's MySQL database..."
        
        self._site_settings_setter()

        user_count = self._fetch_users(cnx)
        print "-> {} Usuarios migrados".format(user_count)
        
        collections_count = self._fetch_collections(cnx)
        print "-> {} Colecciones creadas".format(collections_count)

        categories_count = self._fetch_categories(cnx)
        print "-> {} Categorias migradas".format(categories_count)
        
        articles_count, articles_images, articles_categorizations = self._fetch_content(cnx)
        print "-> {} Articulos migrados".format(articles_count)
        
        categorizations_count = self._categorize_articles(articles_categorizations)
        print "-> {} Articulos categorizados".formar(categorizations_count)
        
        images_count = self._create_images(article_images_hash)
        print "-> {} Imagenes migradas".format(articles_count)
        
        #close mysql connection
        cnx.close()
        
    def _mysql_connection(self, host, database, user, password):
        """Establish a MySQL connection to the given option params and return it"""
        config = {
            'host': host,
            'database': database,
            'user': user
        }
        if password:
            config['password']=password
        try:
            cnx = mysql.connector.connect(**config)
            return cnx
        except mysql.connector.Error as err:
            print err
            raise
        else:
            return cnx

    # QUERIES

    def _fetch_users(self, mysql_cnx):
        """Joomla Users to Cyclope
           Are users treated as authors in Joomla?"""
        fields = ('id', 'username', 'name', 'email', 'registerDate', 'lastvisitDate') # userType
        query = "SELECT {} FROM {}users".format(fields, self.table_prefix)
        query = self._clean_tuple(query)
        cursor = mysql_cnx.cursor()
        cursor.execute(query)
        for user_cursor in cursor:
            user_hash = self._tuples_to_dict(fields, user_cursor)
            user = self._user_to_user(user_hash)
            user.save()
        return User.objects.count()

    def _fetch_content(self, mysql_cnx):
        """Queries Joomla's _content table to populate Articles."""
        articles_images = []
        articles_categorizations = []
        fields = ('title', 'alias', 'introtext', 'fulltext', 'created', 'modified', 'state', 'catid', 'created_by', 'images')
        # we need to quote field names because fulltext is a reserved mysql keyword
        quoted_fields = ["`{}`".format(field) for field in fields]
        query = "SELECT {} FROM {}content".format(quoted_fields, self.table_prefix)
        query = re.sub("[\[\]']", '', query) # clean list and quotes syntax
        cursor = mysql_cnx.cursor()
        cursor.execute(query)
        #single transaction for all articles
        transaction.enter_transaction_management()
        transaction.managed(True)
        for content in cursor:
            content_hash = self._tuples_to_dict(fields, content)
            article = self._content_to_article(content_hash)
            article.save()
            # this is here to have a single query to the largest table
            articles_categorizations.append( self._categorize_object(article, content_hash['catid'], 'article') )
            articles_images.append( self._content_to_images(content_hash) )
        cursor.close()
        transaction.commit()
        transaction.leave_transaction_management()
        return Article.objects.count(), articles_images, articles_categorizations

    def _fetch_collections(self, mysql_cnx):
        """Creates Collections infering them from Categories extensions."""
        query = "SELECT DISTINCT(extension) FROM {}categories".format(self.table_prefix)
        cursor = mysql_cnx.cursor()
        cursor.execute(query)
        for extension in cursor:
            collection = self._category_extension_to_collection(extension[0])
            if collection:
                collection.save()
        cursor.close()
        return Collection.objects.count()

    def _fetch_categories(self, mysql_cnx):
        """Queries Joomla's categories table to populate Categories."""
        fields = ('id', 'path', 'title', 'alias', 'description', 'published', 'parent_id', 'lft', 'rgt', 'level', 'extension')
        query = "SELECT {} FROM {}categories".format(fields, self.table_prefix)
        query = self._clean_tuple(query)
        cursor = mysql_cnx.cursor()
        cursor.execute(query)
        # save categorties in bulk so it doesn't call custom Category save, which doesn't allow custom ids
        categories = []
        for content in cursor:
            category_hash = self._tuples_to_dict(fields, content)
            counter = 1
            category = self._category_to_category(category_hash, counter)
            if category:
                categories.append(category)
                counter += 1
        cursor.close()
        # find duplicate names, since AutoSlugField doesn't properly preserve uniqueness in bulk.
        try: # duplicate query is expensive, we try not to perform it if we can
            Category.objects.bulk_create(categories)
        except IntegrityError:
            cursor = mysql_cnx.cursor()
            query = "SELECT id FROM {}categories WHERE title IN (SELECT title FROM {}categories GROUP BY title HAVING COUNT(title) > 1)".format(self.table_prefix, self.table_prefix)
            cursor.execute(query)
            result = [x[0] for x in cursor.fetchall()]
            cursor.close()
            duplicates = [cat for cat in categories if cat.id in result]
            for dup in duplicates: categories.remove(dup)
            # sort duplicate categories by name ignoring case
            duplicates.sort(key = lambda cat: operator.attrgetter('name')(cat).lower(), reverse=False)
            # categories can have the same name if they're different collections, but not the same slug
            duplicates = self._dup_categories_slugs(duplicates)
            # categories with the same collection cannot have the same name
            duplicates = self._dup_categories_collections(duplicates)
            categories += duplicates
            Category.objects.bulk_create(categories)
        # set MPTT fields using django-mptt's own method TODO
        #Category.tree.rebuild()
        return Category.objects.count()

    def _create_images(self, images):
        pictures = [self._image_to_picture(image_hash) for image_hash in images]
        Picture.objects.bulk_create(pictures)
        return Picture.objects.count()

    def _categorize_articles(self, categorizations):
        Categorization.objects.bulk_create(categorizations)
        return Categorization.objects.count()

    # HELPERS

    def _site_settings_setter(self):
        settings = SiteSettings.objects.all()[0]
        site = settings.site
        if not self.devel_url:
            site.domain = "www.redecom.com.ar" # TODO query
        else:
            site.domain = "localhost:8000"
    
    def _clean_tuple(self, query):
        """clean tuple and quotes syntax"""
        return re.sub("[\(\)']", '', query)
    
    def _tuples_to_dict(self, fields, results):
        return dict(zip(fields, results))

    def _extension_to_collection(self, extension):
        """Single mapping from Joomla extension to Cyclope collection."""
        if extension == 'com_content':
            return (1, 'Contenidos', ['article',])
        else: # We might want to create other collections for newsfeeds, etc.
            return (None, None, None)

    def _dup_categories_slugs(self, categories):
        #use a counter to differentiate them
        counter = 2
        for idx, category in enumerate(categories):
            if idx == 0 :
                category.slug = slugify(category.name)
            else:
                if categories[idx-1].name.lower() == category.name.lower() :
                    category.slug = slugify(category.name) + '-' + str(counter)
                    counter += 1
                else:
                    counter = 2
                    category.slug = slugify(category.name)
        return categories

    def _dup_categories_collections(self, categories):
        counter = 1
        for idx, category in enumerate(categories):
            if idx != 0 :
                if categories[idx-1].name.lower() == category.name.lower() :
                    if categories[idx-1].collection == category.collection :
                        category.name = category.name + " (" + str(counter) + ")"
                else : counter = 1
        return categories

    # JOOMLA'S LOGIC

    def _joomla_content(self, content):
        """Joomla's Read More feature separates content in two columns: introtext and fulltext,
           Most of the time all of the content sits at introtext, but when Read More is activated,
           it is spwaned through both introtext and fulltext.
           Receives the context hash."""
        article_content = content['introtext']
        if content['fulltext']:
            article_content += content['fulltext']
        return article_content

    def _content_to_images(self, content_hash):
        """Instances images from content table's images column or HTML img tags in content.
           Images column has the following JSON '{"image_intro":"","float_intro":"","image_intro_alt":"","image_intro_caption":"","image_fulltext":"","float_fulltext":"","image_fulltext_alt":"","image_fulltext_caption":""}'
           """
        imagenes = []
        # instances images from column
        images = content_hash['images']
        images = json.loads(images)
        # TODO captions, we could insert image_fulltext inside text?
        if images['image_intro']:
            imagenes.append({'src': images['image_intro'], 'alt': images['image_intro_alt']})
        if images['image_fulltext']:
            imagenes.append({'src': images['image_fulltext'], 'alt': images['image_fulltext_alt']})
        # instances images from content
        full_content = self._joomla_content(content_hash)
        tree = html.fromstring(full_content)
        sel = CSSSelector('img')
        imgs = sel(tree)
        for img in imgs:
            src = img.get('src')
            alt = img.get('alt')
            imagenes.append({'src': src, 'alt': alt})
        return imagenes

    # MODELS CONVERSION

    def _content_to_article(self, content):
        """Instances an Article object from a Content hash."""
        article = Article(
            name = content['title'],
            slug = content['alias'], # TODO or AutoSlug?
            creation_date = content['created'] if content['created'] else datetime.now(),
            modification_date = content['modified'],
            date = content['created'],
            published = content['state']==1, # 0=unpublished, 1=published, -1=archived, -2=marked for deletion
            text =  self._joomla_content(content),
            user_id = content['created_by']
        )
        return article

    def _image_to_picture(self, image_hash):
        src = image_hash['src']
        alt = image_hash['alt']
        # TODO URL 
        name = slugify(src)
        picture = Picture(
            image = src,
            description = alt,
            name = name,
            # creation_date = post['post_date'], article
        )
        return picture

    def _category_extension_to_collection(self, extension):
        """Instances a Collection from a Category extension."""
        id, name, types = self._extension_to_collection(extension)
        if id != None:
            collection = Collection.objects.create(id=id, name=name)
            collection.content_types = [ContentType.objects.get(model=content_type) for content_type in types]
            return collection

    def _category_to_category(self, category_hash, counter):
        """Instances a Category in Cyclope from Joomla's Categories table fields."""
        collection_id, name, types = self._extension_to_collection(category_hash['extension'])
        if collection_id: # bring categories for content only
            return Category(
                id = category_hash['id'], # keep ids for foreign keys
                collection_id = collection_id,
                name = category_hash['title'],
                slug = category_hash['path'], # TODO or alias?
                active = category_hash['published']==1,
                parent_id = category_hash['parent_id'] if category_hash['parent_id'] != 0 else None,
                # Cyclope and Joomla use the same tree algorithm
                lft = category_hash['lft'],
                rght = category_hash['rgt'],
                level = category_hash['level'],
                tree_id = counter, # TODO
            )
    
    def _categorize_object(self, objeto, cat_id, model):
        categorization = Categorization(
            category_id = cat_id,
            content_type_id = ContentType.objects.get(model=model).pk,
            object_id = objeto.pk
        )
        return categorization
        
    def _user_to_user(self, user_hash):
        user = User(
            id = user_hash['id'],
            username = user_hash['username'],
            first_name = user_hash['name'],
            email = user_hash['email'],
            is_staff=True,
            is_active=True,
            is_superuser=True, # else doesn't have any permissions
            last_login = user_hash['lastvisitDate'] if user_hash['lastvisitDate'] else datetime.now(),
            date_joined = user_hash['registerDate'],
        )
        password = self.joomla_password if self.joomla_password else user.username
        user.set_password(password)
        return user
