import logging
import requests
from io import BytesIO
from PIL import Image, ExifTags
from boto import s3
from boto.s3.key import Key


log = logging.getLogger(__name__)


class S3ImageResizerException(Exception):
    pass

class InvalidParameterException(S3ImageResizerException):
    pass

class CantFetchImageException(S3ImageResizerException):
    pass

class RTFMException(S3ImageResizerException):
    pass


class S3ImageResizer(object):

    def __init__(self, s3_conn):
        if not s3_conn or 'S3Connection' not in str(type(s3_conn)):
            raise InvalidParameterException("Expecting an instance of boto s3 connection")
        self.s3_conn = s3_conn
        self.image = None
        self.exif_tags = {}


    def fetch(self, url):
        """Fetch an image and keep it in memory"""
        assert url
        log.debug("Fetching image at url %s" % url)
        res = requests.get(url)
        if res.status_code != 200:
            raise CantFetchImageException("Failed to load image at url %s" % url)
        image = Image.open(BytesIO(res.content))

        # Fetch exif tags (if any)
        if image._getexif():
            tags = dict((ExifTags.TAGS[k].lower(), v) for k, v in list(image._getexif().items()) if k in ExifTags.TAGS)
            self.exif_tags = tags

        # Make sure Pillow does not ignore alpha channels during conversion
        # See http://twigstechtips.blogspot.se/2011/12/python-converting-transparent-areas-in.html
        image = image.convert("RGBA")

        canvas = Image.new('RGBA', image.size, (255,255,255,255))
        canvas.paste(image, mask=image)
        self.image = canvas

        return self


    def orientate(self):
        """Apply exif orientation, if any"""

        log.debug("Image has exif tags: %s" % self.exif_tags)

        # No exif orientation?
        if 'orientation' not in self.exif_tags:
            log.info("No exif orientation known for this image")
            return self

        # If image has an exif rotation, apply it to the image prior to resizing
        # See http://stackoverflow.com/questions/4228530/pil-thumbnail-is-rotating-my-image

        angle = self.exif_tags['orientation']
        log.info("Applying exif orientation %s to image" % angle)
        angle_to_degrees = [
            # orientation = transformation
            lambda i: i,
            lambda i: i.transpose(Image.FLIP_LEFT_RIGHT),
            lambda i: i.transpose(Image.ROTATE_180),
            lambda i: i.transpose(Image.FLIP_TOP_BOTTOM),
            lambda i: i.transpose(Image.ROTATE_90).transpose(Image.FLIP_LEFT_RIGHT),
            lambda i: i.transpose(Image.ROTATE_270),
            lambda i: i.transpose(Image.ROTATE_90).transpose(Image.FLIP_TOP_BOTTOM),
            lambda i: i.transpose(Image.ROTATE_90),
        ]

        assert angle >= 1 and angle <= 8
        f = angle_to_degrees[angle - 1]
        self.image = f(self.image)
        return self


    def resize(self, width=None, height=None):
        """Resize the in-memory image previously fetched, and
        return a clone of self holding the resized image"""
        if not width and not height:
            raise InvalidParameterException("One of width or height must be specified")
        if not self.image:
            raise RTFMException("No image loaded! You must call fetch() before resize()")

        cur_width = self.image.width
        cur_height = self.image.height

        if width and height:
            to_width = width
            to_height = height
        elif width:
            to_width = width
            to_height = int(cur_height * width / cur_width)
        elif height:
            to_width = int(cur_width * height / cur_height)
            to_height = height

        # Return a clone of self, loaded with the resized image
        clone = S3ImageResizer(self.s3_conn)
        log.info("Resizing image from (%s, %s) to (%s, %s)" % (cur_width, cur_height, to_width, to_height))
        clone.image = self.image.resize((to_width, to_height), Image.ANTIALIAS)

        return clone


    def store(self, in_bucket=None, key_name=None, metadata=None, quality=95, public=True):
        """Store the loaded image into the given bucket with the given key name. Tag
        it with metadata if provided. Make the Image public and return its url"""
        if not in_bucket:
            raise InvalidParameterException("No in_bucket specified")
        if not key_name:
            raise InvalidParameterException("No key_name specified")
        if not self.image:
            raise RTFMException("No image loaded! You must call fetch() before store()")

        if metadata:
            if type(metadata) is not dict:
                raise RTFMException("metadata must be a dict")
        else:
            metadata = {}

        metadata['Content-Type'] = 'image/jpeg'

        log.info("Storing image into bucket %s/%s" % (in_bucket, key_name))

        # Export image to a string
        sio = BytesIO()
        self.image.save(sio, 'JPEG', quality=quality, optimize=True)
        contents = sio.getvalue()
        sio.close()

        # Get the bucket
        bucket = self.s3_conn.get_bucket(in_bucket)

        # Create a key containing the image. Make it public
        k = Key(bucket)
        k.key = key_name
        k.set_contents_from_string(contents)
        k.set_remote_metadata(metadata, {}, True)

        if public:
            k.set_acl('public-read')

        # Return the key's url
        return k.generate_url(
            method='GET',
            expires_in=0,
            query_auth=False,
            force_http=False
        )
