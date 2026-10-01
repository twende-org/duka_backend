from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """Page-number pagination that lets clients choose the page size.

    The frontend adapter pages through DRF the way it paged through Firestore's
    limit/startAfter, so a bounded ``?page_size=`` is required.
    """

    page_size_query_param = 'page_size'
    max_page_size = 200
